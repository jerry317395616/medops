"""Prepare complete native purchase/related-budget state; never authorize a write."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import frappe

from medops.common.budget_control import KEY_FIELDS, MAX_CONTROL_KEYS, budget_control_key, ordered_budget_keys
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.material_source_snapshot import material_source_snapshot, parse_material_source_snapshot
from medops.common.organization import normalize_reference
from medops.common.reservation_commitment import (
	ORDER_STATUSES,
	ReservationCommitmentIntent,
	reservation_commitment_intent,
)
from medops.hrp_budget.services import budget_usage_observation as access
from medops.hrp_budget.services.approval import require_web_session, session_digest
from medops.hrp_budget.services.control_guards import (
	MAX_BOUND_MATERIAL_SOURCES,
	assert_budget_guards,
	assert_material_guard_bindings,
	invalidate_budget_guards,
)
from medops.hrp_budget.services.control_sources import resolve_budget_control_key
from medops.hrp_budget.services.material_budget_guards import MaterialSourceIdentity
from medops.hrp_budget.services.material_request_source import SOURCE_FIELDS, UNIT_FIELDS
from medops.hrp_budget.services.material_usage_preview import (
	PurchaseUsageInspection,
	inspect_existing_purchase_usage,
)
from medops.hrp_budget.services.material_usage_sources import (
	MAX_ROOTS_PER_SOURCE,
	discover_material_usage_sources,
)
from medops.hrp_budget.services.planning import native_document
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.purchase_order_source import (
	ORDER_FIELDS,
	PurchaseOrderSource,
	locked_purchase_order_source,
)
from medops.hrp_budget.services.release_execution import refresh_financial_authority
from medops.hrp_budget.services.reservation_commitment_allocations import (
	PurchaseCommitmentCapacity,
	read_guarded_commitment_records,
	read_purchase_commitment_pairs,
	verify_guarded_commitment_capacity,
)
from medops.hrp_budget.services.reservation_release_state import read_reservation_original
from medops.services.data_access import require_scope
from medops.services.errors import raise_ione_error, require_roles


def commitment_session(reservation):
	return session_digest("budget-reservation-commitment:" + reservation)


def _actor(actor):
	require_web_session()
	refresh_financial_authority(actor)
	require_roles(MANAGER_ROLES)
	if frappe.session.user != actor:
		raise_ione_error("CONFLICT")


def _source_keys(reference, actor):
	parent = native_document("Material Request", reference.request, SOURCE_FIELDS)
	unit = native_document("HRP Organization Unit", reference.organization_unit, UNIT_FIELDS)
	if (
		parent.name != reference.request
		or unit.name != reference.organization_unit
		or parent.company != unit.company
	):
		raise ValueError("canonical source and organization required")
	access.check_usage_access([(unit.company, unit.hospital, unit.name)], actor)
	indexes = frappe.db.sql(
		"SELECT 1 FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
		"AND TABLE_NAME=%s AND COLUMN_NAME=%s AND SEQ_IN_INDEX=1 LIMIT 1",
		("tabHRP Budget Reservation", "source_name"),
	)
	if not indexes:
		raise_ione_error("CONFIGURATION_INVALID")
	columns = ", ".join(f"`{name}`" for name in sorted(KEY_FIELDS))
	rows = frappe.db.sql(
		f"SELECT {columns} FROM `tabHRP Budget Reservation` "  # nosec B608 - fixed fields/bound native source
		"WHERE `source_doctype`=%s AND `source_name`=%s AND `source_line`=%s ORDER BY `name` LIMIT %s",
		("Material Request", reference.request, reference.line, MAX_ROOTS_PER_SOURCE + 1),
		as_dict=True,
	)
	if len(rows) > MAX_ROOTS_PER_SOURCE:
		raise ValueError("source reservation discovery exceeds bound")
	keys = tuple(budget_control_key(dict(row)) for row in rows)
	access.check_usage_access(
		sorted({(key.company, key.hospital, key.organization_unit) for key in keys}), actor
	)
	return keys


def discover_commitment_scope(original, order, line, actor):
	"""Close all related source/key references before the first parent/budget lock.

	The returned set is untrusted discovery. Current physical membership and all
	independent histories must be re-read after prelocking; a change can require
	whole-transaction retry. No lock upgrades or incremental budget locks occur.
	"""
	if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is not None:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	facts = parse_material_source_snapshot(
		original.source_snapshot, expected_digest=original.source_snapshot_digest
	)
	selected = MaterialSourceIdentity(facts["request"], facts["line"], original.key.organization_unit)
	keys = {original.key}
	for item, _, _, _, _ in read_purchase_commitment_pairs(order, line):
		keys.add(item.key)
	expanded = set()
	for _ in range(MAX_BOUND_MATERIAL_SOURCES + 1):
		if len(keys) > MAX_CONTROL_KEYS:
			raise ValueError("related budget key discovery exceeds bound")
		ordered = ordered_budget_keys(tuple(keys))
		for key in ordered:
			if resolve_budget_control_key(asdict(key)) != key:
				raise ValueError("persisted budget references are not canonical")
		refs = discover_material_usage_sources(ordered, historical=True)
		identities = {MaterialSourceIdentity(r.request, r.line, r.organization_unit) for r in refs} | {
			selected
		}
		if len(identities | expanded) > MAX_BOUND_MATERIAL_SOURCES:
			raise ValueError("related material source discovery exceeds bound")
		new = identities - expanded
		before = set(keys)
		for ref in sorted(new, key=lambda r: (r.request, r.line, r.organization_unit)):
			keys.update(_source_keys(ref, actor))
			expanded.add(ref)
		_actor(actor)
		if keys == before and not new:
			return ordered
	raise ValueError("related source discovery did not converge")


@dataclass(frozen=True, slots=True)
class LockedCommitmentState:
	inspection: PurchaseUsageInspection = field(repr=False)
	purchase: PurchaseOrderSource = field(repr=False)
	item: ReservationCommitmentIntent = field(repr=False)
	capacity: PurchaseCommitmentCapacity
	actor: str
	session: str = field(repr=False)


def inspect_commitment_history(reservation, order, line):
	"""Prepare full historical scope, preserving current reviewed PO progress states.

	This does not assert that the current state permits any new commitment.
	"""
	try:
		actor = frappe.session.user
		_actor(actor)
		if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is not None:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		for value, label in ((order, "purchase_order"), (line, "purchase_order_line")):
			if normalize_reference(value, label=label) != value:
				raise ValueError("canonical native purchase identity required")
		_, original, _, _ = read_reservation_original(reservation)
		facts = parse_material_source_snapshot(
			original.source_snapshot, expected_digest=original.source_snapshot_digest
		)
		doc = native_document("Purchase Order", order, ORDER_FIELDS)
		items = [item for item in doc.items if item.name == line]
		if (
			doc.name != order
			or doc.company != original.key.company
			or len(items) != 1
			or (items[0].material_request, items[0].material_request_item)
			!= (facts["request"], facts["line"])
		):
			raise ValueError("native purchase order differs from selected material request")
		_actor(actor)
		keys = discover_commitment_scope(original, order, line, actor)
		inspection = inspect_existing_purchase_usage(
			facts["request"],
			facts["line"],
			original.key.organization_unit,
			budget_date=original.budget_date,
			proposed_keys=[asdict(key) for key in keys],
			purchase_parents=(order,),
		)
		source, purchase = locked_purchase_order_source(
			order,
			line,
			facts["request"],
			facts["line"],
			original.key.organization_unit,
			expected_request_digest=inspection.source.digest(),
			historical=True,
		)
		if source != inspection.source:
			raise ValueError("prepared material source changed")
		_actor(actor)
		return actor, original, inspection, purchase
	except (TypeError, ValueError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise


def inspect_reservation_commitment(reservation, order, line, *, amount, reason):
	"""Prepare native full scope and exact intent in a clean owned transaction."""
	try:
		actor, original, inspection, purchase = inspect_commitment_history(reservation, order, line)
		if purchase.status not in ORDER_STATUSES:
			raise ValueError("new commitment requires an open submitted purchase order")
		source = inspection.source
		keys = inspection.keys
		positions = [p for p in inspection.source_usage if p.reservation == reservation]
		if len(positions) != 1:
			raise ValueError("one complete selected reservation required")
		records = read_guarded_commitment_records(keys, reservation, positions[0].last_sequence)
		material, digest = material_source_snapshot(asdict(source))
		order_snapshot, order_digest = purchase.commitment_snapshot(source)
		item = reservation_commitment_intent(
			original,
			records,
			order_snapshot=order_snapshot,
			order_digest=order_digest,
			material_snapshot=material,
			material_digest=digest,
			amount=exact_decimal_amount(amount, original.precision, maximum=MAX_APPROVAL_AMOUNT),
			reason=reason,
		)
		capacity = verify_guarded_commitment_capacity(inspection, purchase, item)
		_actor(actor)
		return LockedCommitmentState(
			inspection, purchase, item, capacity, actor, commitment_session(item.reservation)
		)
	except (TypeError, ValueError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise


def check_commitment_state_access(state, *, historical: bool = False):
	"""Refresh current rights and exact sources at each controller stage.

	This does not replace the full immutable history/capacity checks on entry
	and exit of the complete persistence operation. Completed receipt reads opt
	into historical current states; every new controller stage defaults to open.
	"""
	if type(state) is not LockedCommitmentState or type(historical) is not bool:
		raise ValueError("native locked commitment state required")
	_actor(state.actor)
	if commitment_session(state.item.reservation) != state.session:
		raise ValueError("commitment session changed")
	keys = state.inspection.keys
	bindings = tuple(
		sorted((s.request, s.line, s.organization_unit, s.digest()) for s in state.inspection.sources)
	)
	assert_material_guard_bindings(keys, bindings)
	access.check_usage_access(
		sorted({(k.company, k.hospital, k.organization_unit) for k in keys}), state.actor
	)
	require_scope(
		"HRP Budget Reservation",
		company=state.item.key.company,
		hospital=state.item.key.hospital,
		organization_unit=state.item.key.organization_unit,
		action="write",
	)
	source = state.inspection.source
	if (source, state.purchase) != locked_purchase_order_source(
		state.purchase.order,
		state.purchase.line,
		source.request,
		source.line,
		source.organization_unit,
		expected_request_digest=source.digest(),
		historical=historical,
	):
		raise ValueError("commitment source changed")
	_actor(state.actor)
	assert_material_guard_bindings(keys, bindings)


def recheck_reservation_commitment(state):
	"""Independently repeat full history/capacity and current actor/session checks."""
	try:
		check_commitment_state_access(state)
		capacity = verify_guarded_commitment_capacity(state.inspection, state.purchase, state.item)
		if capacity != state.capacity:
			raise ValueError("purchase capacity observation changed")
		assert_budget_guards(state.inspection.keys)
		_actor(state.actor)
		return state
	except (TypeError, ValueError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
