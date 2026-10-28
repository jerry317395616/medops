"""Authenticate frozen roots and complete release histories under shared guards."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import frappe

from medops.common.authorized_reservation_projection import verify_authorized_released_reservation
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS
from medops.common.budget_reservation_events import ReservationPosition
from medops.common.budget_reservation_proof import ReservationRecord, reservation_event_key
from medops.common.budget_reservation_storage import reservation_record_from_row
from medops.common.domain_service import normalize_sha256
from medops.common.initial_reservation_posting import (
	initial_reservation_documents,
	renewed_initial_reservation_documents,
)
from medops.common.material_source_snapshot import parse_material_source_snapshot
from medops.common.reservation_authorization import (
	AUTHORIZATION_ROW_FIELDS,
	ReservationIntent,
	verify_reservation_authorization_rows,
)
from medops.hrp_budget.services import budget_usage_observation as access
from medops.hrp_budget.services.approval import require_web_session, session_digest
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.initial_reservation_write import check_persisted_document
from medops.hrp_budget.services.material_budget_guards import assert_material_budget_guards
from medops.hrp_budget.services.material_request_source import (
	MaterialRequestSource,
	locked_material_request_source,
)
from medops.hrp_budget.services.material_usage_preview import inspect_existing_purchase_usage
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.reservation_confirmation import require_reservation_actor_scope
from medops.hrp_budget.services.reservation_receipt_history import read_locked_initial_receipt_history
from medops.hrp_budget.services.reservation_renewal_lineage import locked_renewal_lineage
from medops.services.data_access import require_scope
from medops.services.errors import raise_ione_error, require_roles

ROOT = "HRP Budget Reservation"
EVENT = "HRP Budget Reservation Event"
AUTHORIZATION = "HRP Budget Reservation Authorization"


@dataclass(frozen=True, slots=True)
class LockedReservationReleaseState:
	original: ReservationIntent = field(repr=False)
	source: MaterialRequestSource = field(repr=False)
	position: ReservationPosition
	records: tuple[ReservationRecord, ...] = field(repr=False)


def _original(root, *, lock):
	require_scope(
		ROOT,
		company=root.company,
		hospital=root.hospital,
		organization_unit=root.organization_unit,
		action="write",
	)
	names = frappe.get_all(
		AUTHORIZATION,
		filters={"reservation_identity": root.source_identity_key, "event_kind": "Decision"},
		pluck="name",
		limit_page_length=2,
	)
	if len(names) != 1:
		raise ValueError("one original decision required for release")
	decision = frappe.get_doc(AUTHORIZATION, names[0], for_update=lock)
	confirmation = frappe.get_doc(AUTHORIZATION, decision.confirmation, for_update=lock)
	first = {name: confirmation.get(name) for name in AUTHORIZATION_ROW_FIELDS}
	second = {name: decision.get(name) for name in AUTHORIZATION_ROW_FIELDS}
	item, _ = verify_reservation_authorization_rows(first, second)
	if item.reservation_identity != root.source_identity_key or (
		item.key.company,
		item.key.hospital,
		item.key.organization_unit,
	) != (root.company, root.hospital, root.organization_unit):
		raise ValueError("release root differs from original decision scope")
	return item, first, second


def check_release_state_access(state):
	actor = frappe.session.user
	require_web_session()
	require_reservation_actor_scope(
		state.original, actor, session_digest("budget-reservation:" + state.original.reservation_identity)
	)
	key = state.original.key
	access.check_usage_access([(key.company, key.hospital, key.organization_unit)], actor)
	assert_material_budget_guards(state.source, [key])
	if (
		locked_material_request_source(
			state.source.request, state.source.line, state.source.organization_unit
		)
		!= state.source
	):
		raise ValueError("release source changed")
	assert_material_budget_guards(state.source, [key])


def _locked_state(reservation, item, source, position):
	provisional = LockedReservationReleaseState(item, source, position, ())
	check_release_state_access(provisional)
	root = frappe.get_doc(ROOT, reservation, for_update=True)
	checked, first, second = _original(root, lock=True)
	if checked != item:
		raise ValueError("release original intent changed")
	lineage = locked_renewal_lineage(item, source, first["name"], second["name"], allow_progress=True)
	rows, releases = read_locked_initial_receipt_history(
		item, source, reservation, reservation_event_key(reservation, 1), observed_position=position
	)
	records = tuple(reservation_record_from_row(row) for row in rows)
	documents = (
		renewed_initial_reservation_documents(
			first, second, lineage.renewals, occurred_at=records[0].occurred_at
		)
		if lineage.renewals
		else initial_reservation_documents(first, second, occurred_at=records[0].occurred_at)
	)
	check_persisted_document(root, documents.root, 1)
	check_persisted_document(
		frappe.get_doc(EVENT, records[0].event.name, for_update=True), documents.event, 0
	)
	root_values = {name: root.get(name) for name in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}}
	actual, _, _ = verify_authorized_released_reservation(
		root_values, rows, first, second, releases, current_facts=asdict(source), renewals=lineage.renewals
	)
	if actual != position:
		raise ValueError("release history differs from inspected position")
	state = LockedReservationReleaseState(item, source, actual, records)
	check_release_state_access(state)
	return state


def inspect_reservation_release(reservation):
	"""Start a clean owned transaction's full purchase inspection; never authorize a write."""
	try:
		require_roles(MANAGER_ROLES)
		require_web_session()
		if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is not None:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		name = normalize_sha256(reservation, label="reservation")
		root = frappe.get_doc(ROOT, name)
		item, _, _ = _original(root, lock=False)
		facts = parse_material_source_snapshot(
			item.source_snapshot, expected_digest=item.source_snapshot_digest
		)
		inspection = inspect_existing_purchase_usage(
			facts["request"],
			facts["line"],
			item.key.organization_unit,
			budget_date=item.budget_date,
			proposed_keys=[asdict(item.key)],
		)
		positions = [position for position in inspection.source_usage if position.reservation == name]
		if len(positions) != 1:
			raise ValueError("one fully inspected reservation required")
		return _locked_state(name, item, inspection.source, positions[0])
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise


def recheck_reservation_release(state, *, position=None):
	"""Re-read all independent history using the current or just-appended exact position."""
	try:
		if type(state) is not LockedReservationReleaseState:
			raise ValueError("checked release state required")
		expected = state.position if position is None else position
		return _locked_state(state.position.reservation, state.original, state.source, expected)
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
