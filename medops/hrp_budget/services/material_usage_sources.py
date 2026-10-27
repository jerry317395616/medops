"""Discover and recheck purchase references; never authorize budget spending."""

from __future__ import annotations

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


def _roots(keys, actor):
	scopes = sorted({observation._scope(key) for key in keys})
	units = sorted({(key.company, key.hospital, key.organization_unit) for key in keys})
	observation._access(units, actor)
	rows = observation._scope_query(
		"tabHRP Budget Reservation", ANCHOR_ROW_FIELDS, scopes, observation.MAX_USAGE_ROOTS
	)
	observation._access(units, actor)
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


def guarded_verify_material_usage_sources(value: object, sources: object):
	"""Recheck the complete discovered set against currently locked native rows.

	Only proves current source facts match captured digests; normal later changes
	can prevent verification. It does not prove historical approvals, completeness
	of other writers, allocation policy, authenticity against database tampering,
	or funds. Caller must roll back the whole transaction on any failure.
	"""
	try:
		keys = ordered_budget_keys(value)
		if not isinstance(sources, (tuple, list)) or not 1 <= len(sources) <= MAX_BOUND_MATERIAL_SOURCES:
			raise ValueError("bounded locked source snapshots required")
		if any(type(source) is not MaterialRequestSource for source in sources):
			raise ValueError("canonical source snapshots required")
		by_id = {(s.request, s.line, s.organization_unit): s for s in sources}
		if len(by_id) != len(sources):
			raise ValueError("duplicate locked source")
		bindings = tuple(sorted((*identity, source.digest()) for identity, source in by_id.items()))
		assert_material_guard_bindings(keys, bindings)
		rows = _roots(keys, frappe.session.user)
		refs = _references(rows)
		if tuple((r.request, r.line, r.organization_unit, r.expected_digest) for r in refs) != bindings:
			raise ValueError("reservation source set changed")
		for identity, source in by_id.items():
			if locked_material_request_source(*identity) != source:
				raise ValueError("material source changed")
		for row in rows.values():
			source = by_id[(row["source_name"], row["source_line"], row["organization_unit"])]
			if (
				row["source_snapshot_digest"] != source.digest()
				or row["cost_center"] != source.cost_center
				or row["currency"] != source.currency
				or (row["company"], row["hospital"], row["organization_unit"], row["project"] or None)
				!= (source.company, source.hospital, source.organization_unit, source.project)
			):
				raise ValueError("reservation differs from native source")
		positions = observation.guarded_budget_usage_observation(keys)
		if _roots(keys, frappe.session.user) != rows:
			raise ValueError("reservation roots changed during verification")
		assert_material_guard_bindings(keys, bindings)
		return positions
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
