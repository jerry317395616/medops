"""Discover and recheck purchase references; never authorize budget spending."""

from __future__ import annotations

from decimal import Decimal, localcontext

import frappe

from medops.common.budget_control import ordered_budget_keys
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS
from medops.hrp_budget.services import budget_usage_observation as observation
from medops.hrp_budget.services.control_guards import (
	MAX_BOUND_MATERIAL_SOURCES,
	assert_material_guard_bindings,
	invalidate_budget_guards,
)
from medops.hrp_budget.services.material_budget_guards import MaterialSourceReference
from medops.hrp_budget.services.material_request_source import (
	MaterialRequestSource,
	locked_material_request_source,
)
from medops.services.errors import raise_ione_error

MAX_ROOTS_PER_SOURCE = 100
MAX_EVENTS_PER_SOURCE = 10000


def _roots(keys, actor):
	scopes = sorted({observation.usage_scope(key) for key in keys})
	units = sorted({(key.company, key.hospital, key.organization_unit) for key in keys})
	observation.check_usage_access(units, actor)
	rows = observation.query_usage_scope(
		"tabHRP Budget Reservation", ANCHOR_ROW_FIELDS, scopes, observation.MAX_USAGE_ROOTS
	)
	observation.check_usage_access(units, actor)
	return rows


def _references(rows):
	refs = {}
	for row in rows.values():
		if row.get("source_doctype") != "Material Request" or row.get("source_revision") != row.get(
			"source_snapshot_digest"
		):
			raise ValueError("unsupported reservation source")
		ref = MaterialSourceReference(
			row["source_name"], row["source_line"], row["organization_unit"], row["source_snapshot_digest"]
		)
		identity = (ref.request, ref.line, ref.organization_unit)
		if identity in refs and refs[identity] != ref:
			raise ValueError("conflicting source snapshots")
		refs[identity] = ref
		if len(refs) > MAX_BOUND_MATERIAL_SOURCES:
			raise ValueError("too many material sources")
	return tuple(refs[key] for key in sorted(refs))


def _source_root_names(source: MaterialRequestSource) -> frozenset[str]:
	"""Find every physical root for a source, without disclosing its contents."""
	rows = frappe.db.sql(
		"""SELECT `name` FROM `tabHRP Budget Reservation`
		WHERE `source_doctype`=%s AND `source_name`=%s AND `source_line`=%s
		ORDER BY `name` LIMIT %s""",
		("Material Request", source.request, source.line, MAX_ROOTS_PER_SOURCE + 1),
	)
	if len(rows) > MAX_ROOTS_PER_SOURCE:
		raise ValueError("source reservation count exceeds bound")
	names = [row[0] for row in rows]
	if any(not isinstance(name, str) or not name for name in names) or len(names) != len(set(names)):
		raise ValueError("invalid source reservation identity")
	return frozenset(names)


def _require_source_indexes():
	rows = frappe.db.sql(
		"""SELECT DISTINCT TABLE_NAME FROM information_schema.STATISTICS
		WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (%s, %s)
		AND COLUMN_NAME=%s AND SEQ_IN_INDEX=1""",
		("tabHRP Budget Reservation", "tabHRP Budget Reservation Event", "source_name"),
	)
	if {row[0] for row in rows} != {"tabHRP Budget Reservation", "tabHRP Budget Reservation Event"}:
		raise_ione_error("CONFIGURATION_INVALID")


def _check_all_source_roots(rows, sources):
	_require_source_indexes()
	if len({(source.request, source.line) for source in sources}) != len(sources):
		raise ValueError("a purchase line cannot span multiple locked organization units")
	for source in sources:
		guarded = frozenset(
			name
			for name, row in rows.items()
			if (row["source_doctype"], row["source_name"], row["source_line"])
			== ("Material Request", source.request, source.line)
		)
		if _source_root_names(source) != guarded:
			raise ValueError("source has reservation roots outside guarded scopes")
		events = frappe.db.sql(
			"""SELECT `name`, `reservation` FROM `tabHRP Budget Reservation Event`
			WHERE `source_doctype`=%s AND `source_name`=%s AND `source_line`=%s
			ORDER BY `name` LIMIT %s""",
			("Material Request", source.request, source.line, MAX_EVENTS_PER_SOURCE + 1),
		)
		if len(events) > MAX_EVENTS_PER_SOURCE:
			raise ValueError("source event count exceeds bound")
		counts = {name: 0 for name in guarded}
		seen = set()
		for event_name, parent in events:
			if (
				not isinstance(event_name, str)
				or not event_name
				or event_name in seen
				or parent not in counts
			):
				raise ValueError("orphan or duplicate source reservation event")
			seen.add(event_name)
			counts[parent] += 1
		if any(
			type(rows[name]["event_count"]) is not int or rows[name]["event_count"] != count
			for name, count in counts.items()
		):
			raise ValueError("source event count differs from guarded roots")


def discover_material_usage_sources(value: object) -> tuple[MaterialSourceReference, ...]:
	"""Return untrusted references for prelocking, never for financial decisions.

	Discovery must start in the owned transaction before source/master locks.
	The set can change and must be rechecked after every source and budget guard
	is acquired. Empty results are not evidence that there are no orphan events.
	"""
	try:
		if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is not None:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		keys = ordered_budget_keys(value)
		return _references(_roots(keys, frappe.session.user))
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise


def guarded_verify_material_usage_sources(value: object, sources: object, *, discovered: object):
	"""Strictly require every source to match the originally captured digest."""
	return _guarded_verify_material_usage_sources(value, sources, discovered=discovered, historical=False)


def guarded_verify_provisional_material_usage_sources(value: object, sources: object, *, discovered: object):
	"""Return untrusted usage pending separate historical decision verification.

	Current sources are locked but their digest may differ from an original root.
	A caller must authenticate every independent decision and compare original
	financial facts before returning results or attempting any write.
	"""
	return _guarded_verify_material_usage_sources(value, sources, discovered=discovered, historical=True)


def _guarded_verify_material_usage_sources(
	value: object, sources: object, *, discovered: object, historical: bool
):
	"""Recheck discovered usage and all locked sources, including new requests.

	The strict mode requires current source facts to match captured digests; the
	provisional mode leaves historical comparison to its caller. Neither proves
	historical approvals, completeness
	of other writers or missing physical evidence, allocation policy, authenticity
	against database tampering, or funds. The caller must pass the exact pre-lock
	discovery result and include all historical and proposed sources. An empty set
	does not imply no orphan events: the guarded chain inspection still runs.
	Caller must roll back the whole transaction on any failure.
	"""
	try:
		actor = frappe.session.user
		keys = ordered_budget_keys(value)
		if not isinstance(sources, (tuple, list)) or not 1 <= len(sources) <= MAX_BOUND_MATERIAL_SOURCES:
			raise ValueError("bounded locked source snapshots required")
		if any(type(source) is not MaterialRequestSource for source in sources):
			raise ValueError("canonical source snapshots required")
		if (
			not isinstance(discovered, tuple)
			or len(discovered) > MAX_BOUND_MATERIAL_SOURCES
			or any(type(ref) is not MaterialSourceReference for ref in discovered)
		):
			raise ValueError("canonical pre-lock discovery required")
		by_id = {(s.request, s.line, s.organization_unit): s for s in sources}
		if len(by_id) != len(sources):
			raise ValueError("duplicate locked source")
		if any(
			not any(
				(source.company, source.hospital, source.organization_unit, source.project)
				== (key.company, key.hospital, key.organization_unit, key.project)
				for key in keys
			)
			for source in sources
		):
			raise ValueError("locked material source outside guarded scope")
		bindings = tuple(sorted((*identity, source.digest()) for identity, source in by_id.items()))
		assert_material_guard_bindings(keys, bindings)
		rows = _roots(keys, actor)
		refs = _references(rows)
		if refs != discovered:
			raise ValueError("reservation source set changed")
		if any(
			(r.request, r.line, r.organization_unit) not in by_id
			or (
				not historical and (r.request, r.line, r.organization_unit, r.expected_digest) not in bindings
			)
			for r in refs
		):
			raise ValueError("historical reservation source was not locked")
		for identity, source in by_id.items():
			if locked_material_request_source(*identity) != source:
				raise ValueError("material source changed")
		for row in rows.values():
			source = by_id[(row["source_name"], row["source_line"], row["organization_unit"])]
			if (
				(not historical and row["source_snapshot_digest"] != source.digest())
				or row["cost_center"] != source.cost_center
				or row["currency"] != source.currency
				or (row["company"], row["hospital"], row["organization_unit"], row["project"] or None)
				!= (source.company, source.hospital, source.organization_unit, source.project)
			):
				raise ValueError("reservation differs from native source")
		_check_all_source_roots(rows, sources)
		if frappe.session.user != actor:
			raise_ione_error("CONFLICT")
		positions = observation.guarded_budget_usage_observation(keys)
		with localcontext() as context:
			context.prec = 48
			exposure = {identity: Decimal(0) for identity in by_id}
			for position in positions:
				row = rows[position.reservation]
				identity = (row["source_name"], row["source_line"], row["organization_unit"])
				exposure[identity] += (
					Decimal(position.open_reservation)
					+ Decimal(position.open_commitment)
					+ Decimal(position.actual)
				)
			if any(exposure[identity] > Decimal(source.amount) for identity, source in by_id.items()):
				raise ValueError("combined source reservations exceed purchase amount")
		if _roots(keys, actor) != rows:
			raise ValueError("reservation roots changed during verification")
		_check_all_source_roots(rows, sources)
		if frappe.session.user != actor:
			raise_ione_error("CONFLICT")
		assert_material_guard_bindings(keys, bindings)
		return positions
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
