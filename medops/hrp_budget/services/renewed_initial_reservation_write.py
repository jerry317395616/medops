"""Private renewed initial reservation posting; not a public endpoint."""

from __future__ import annotations

import hashlib
from dataclasses import asdict

import frappe
from frappe.utils import now_datetime

from medops.common.authorized_reservation_projection import (
	verify_authorized_released_reservation,
	verify_renewed_initial_reservation,
)
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS
from medops.common.budget_reservation_storage import ROW_FIELDS, reservation_record_from_row
from medops.common.domain_service import DomainServiceDefinition, normalize_sha256
from medops.common.initial_reservation_posting import renewed_initial_reservation_documents
from medops.common.material_source_snapshot import parse_material_source_snapshot
from medops.common.reservation_authorization import (
	AUTHORIZATION_ROW_FIELDS,
	verify_reservation_authorization_rows,
)
from medops.common.reservation_renewal_request import (
	RenewedReservationPostCommand,
	build_renewed_reservation_post,
)
from medops.hrp_budget.services.approval import require_web_session, session_digest
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.initial_reservation_control import (
	EVENT,
	ROOT,
	enabled_renewed_reservation,
	expected_initial_reservation,
)
from medops.hrp_budget.services.initial_reservation_write import (
	check_persisted_document,
	exact_new_document,
	require_initial_identity_available,
)
from medops.hrp_budget.services.material_budget_guards import assert_material_budget_guards
from medops.hrp_budget.services.material_request_source import locked_material_request_source
from medops.hrp_budget.services.material_usage_preview import inspect_existing_purchase_usage
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.reservation_confirmation import (
	DOCTYPE as AUTHORIZATION,
)
from medops.hrp_budget.services.reservation_confirmation import (
	require_reservation_actor_scope,
	revalidate_reservation_intent,
)
from medops.hrp_budget.services.reservation_receipt_history import read_locked_initial_receipt_history
from medops.hrp_budget.services.reservation_renewal_control import read_confirmation
from medops.hrp_budget.services.reservation_renewal_lineage import locked_renewal_lineage
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error


def _evidence(name, *, lock=False):
	doc = frappe.get_doc(AUTHORIZATION, name, for_update=lock)
	return {field: doc.get(field) for field in AUTHORIZATION_ROW_FIELDS}


def _original(command, *, lock=False):
	first = _evidence(command.original_confirmation, lock=lock)
	second = _evidence(command.original_decision, lock=lock)
	item, original = verify_reservation_authorization_rows(first, second)
	return first, second, item, original


def _proof(item, confirmed, proof, *, now):
	from medops.common.reservation_authorization import verify_reservation_confirmation_proof

	session = session_digest("budget-reservation:" + item.reservation_identity)
	try:
		verify_reservation_confirmation_proof(
			item, confirmed, proof=proof, actor=frappe.session.user, session_digest=session, now=now
		)
	except ValueError as error:
		raise_ione_error("PERMISSION_DENIED", cause=error)
	return session


def _latest(command, item, source, *, replay=False, allow_progress=False):
	lineage = locked_renewal_lineage(
		item, source, command.original_confirmation, command.original_decision, allow_progress=allow_progress
	)
	if not lineage.renewals:
		raise_ione_error("CONFLICT")
	confirmed, decision = lineage.renewals[-1]
	if (
		confirmed.name != command.renewal_confirmation
		or decision.name != command.renewal_decision
		or decision.actor != frappe.session.user
	):
		raise_ione_error("CONFLICT")
	now = decision.recorded_at if replay else now_datetime().isoformat(sep=" ", timespec="microseconds")
	session = _proof(item, confirmed, command.proof, now=now)
	return lineage, session


def append_renewed_initial_reservation(command: RenewedReservationPostCommand) -> dict[str, str]:
	"""Append one exact renewed reservation; caller owns the full transaction."""
	try:
		enabled_renewed_reservation()
		require_web_session()
		if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is not None:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		_, _, item, _ = _original(command)
		checked, confirmed = read_confirmation(command.renewal_confirmation)
		if checked != item:
			raise_ione_error("CONFLICT")
		actor = frappe.session.user
		session = _proof(
			item, confirmed, command.proof, now=now_datetime().isoformat(sep=" ", timespec="microseconds")
		)
		revalidate_reservation_intent(item, actor=actor, session=session)
		facts = parse_material_source_snapshot(
			item.source_snapshot, expected_digest=item.source_snapshot_digest
		)
		source = locked_material_request_source(facts["request"], facts["line"], item.key.organization_unit)
		assert_material_budget_guards(source, [item.key])
		first, second, locked_item, original = _original(command, lock=True)
		if locked_item != item:
			raise_ione_error("CONFLICT")
		lineage, checked_session = _latest(command, item, source)
		if lineage.original != original or checked_session != session:
			raise_ione_error("CONFLICT")
		require_initial_identity_available(item, source)
		documents = renewed_initial_reservation_documents(
			first,
			second,
			lineage.renewals,
			occurred_at=now_datetime().isoformat(sep=" ", timespec="microseconds"),
		)
		with expected_initial_reservation(
			documents,
			source,
			(item.key,),
			confirmation=command.original_confirmation,
			decision=command.original_decision,
			proof=command.proof,
			renewal_confirmation=command.renewal_confirmation,
			renewal_decision=command.renewal_decision,
		):
			root = exact_new_document(ROOT, documents.root).insert(ignore_permissions=True)
			root.submit()
			event = exact_new_document(EVENT, documents.event).insert(ignore_permissions=True)
		root = frappe.get_doc(ROOT, root.name, for_update=True)
		event = frappe.get_doc(EVENT, event.name, for_update=True)
		check_persisted_document(root, documents.root, 1)
		check_persisted_document(event, documents.event, 0)
		final_first, final_second, final_item, final_original = _original(command, lock=True)
		final_lineage, final_session = _latest(command, item, source)
		if (
			final_item != item
			or final_original != original
			or final_lineage != lineage
			or final_session != session
		):
			raise_ione_error("CONFLICT")
		root_row = {field: root.get(field) for field in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}}
		event_row = {field: event.get(field) for field in ROW_FIELDS}
		verify_renewed_initial_reservation(
			root_row,
			[event_row],
			final_first,
			final_second,
			final_lineage.renewals,
			current_facts=asdict(source),
		)
		revalidate_reservation_intent(item, actor=actor, session=session)
		assert_material_budget_guards(source, [item.key])
		return {"reservation": root.name, "event": event.name, "intent_digest": item.intent_digest}
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise


def verify_existing_renewed_reservation(
	command: RenewedReservationPostCommand, result: dict[str, object]
) -> None:
	"""Recheck existing evidence and current access on initial success and replay."""
	try:
		enabled_renewed_reservation()
		require_web_session()
		if set(result) != {"reservation", "event", "intent_digest"}:
			raise ValueError("complete reservation result required")
		for field in ("reservation", "event", "intent_digest"):
			normalize_sha256(result[field], label=field)
		_, _, item, original = _original(command)
		actor = frappe.session.user
		session = session_digest("budget-reservation:" + item.reservation_identity)
		require_reservation_actor_scope(item, actor, session)
		facts = parse_material_source_snapshot(
			item.source_snapshot, expected_digest=item.source_snapshot_digest
		)
		observed_position = None
		if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is None:
			inspection = inspect_existing_purchase_usage(
				facts["request"],
				facts["line"],
				item.key.organization_unit,
				budget_date=item.budget_date,
				proposed_keys=[asdict(item.key)],
			)
			source = inspection.source
			matches = [p for p in inspection.source_usage if p.reservation == result["reservation"]]
			if len(matches) != 1:
				raise ValueError("posted reservation is not in the checked source usage")
			observed_position = matches[0]
		else:
			source = locked_material_request_source(
				facts["request"], facts["line"], item.key.organization_unit
			)
			assert_material_budget_guards(source, [item.key])
		first, second, locked_item, locked_original = _original(command, lock=True)
		lineage, checked_session = _latest(command, item, source, replay=True, allow_progress=True)
		if (
			locked_item != item
			or locked_original != original
			or lineage.original != original
			or checked_session != session
		):
			raise ValueError("renewal authorization changed")
		root = frappe.get_doc(ROOT, result["reservation"], for_update=True)
		event_rows, releases, commitments = read_locked_initial_receipt_history(
			item, source, root.name, result["event"], observed_position=observed_position
		)
		event = frappe.get_doc(EVENT, result["event"], for_update=True)
		record = reservation_record_from_row(event_rows[0])
		documents = renewed_initial_reservation_documents(
			first, second, lineage.renewals, occurred_at=record.occurred_at
		)
		check_persisted_document(root, documents.root, 1)
		check_persisted_document(event, documents.event, 0)
		root_row = {field: root.get(field) for field in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}}
		position, _, checked = verify_authorized_released_reservation(
			root_row,
			event_rows,
			first,
			second,
			releases,
			renewals=lineage.renewals,
			commitments=commitments,
			current_facts=asdict(source),
		)
		if (
			result["intent_digest"] != item.intent_digest
			or checked.name != command.renewal_decision
			or position.reserved != item.amount
		):
			raise ValueError("posted reservation differs from renewal")
		if locked_material_request_source(source.request, source.line, source.organization_unit) != source:
			raise ValueError("purchase source changed")
		require_reservation_actor_scope(item, actor, session)
		assert_material_budget_guards(source, [item.key])
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise


class PostRenewedInitialReservation(DomainService[RenewedReservationPostCommand]):
	definition = DomainServiceDefinition(
		name="hrp_budget.reservation.post_renewed", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		require_web_session()
		enabled_renewed_reservation()

	def validate(self, command):
		try:
			if (
				type(command) is not RenewedReservationPostCommand
				or build_renewed_reservation_post(
					{
						"original_confirmation": command.original_confirmation,
						"original_decision": command.original_decision,
						"renewal_confirmation": command.renewal_confirmation,
						"renewal_decision": command.renewal_decision,
						"proof": command.proof,
						"acknowledge": command.acknowledge,
					}
				)
				!= command
			):
				raise ValueError("canonical renewed reservation command required")
		except ValueError as error:
			raise_ione_error("INVALID_REQUEST", cause=error)
		self.command = command

	def request_payload(self, command):
		return {
			"original_confirmation": command.original_confirmation,
			"original_decision": command.original_decision,
			"renewal_confirmation": command.renewal_confirmation,
			"renewal_decision": command.renewal_decision,
			"proof_hash": hashlib.sha256(command.proof.encode()).hexdigest(),
			"acknowledge": True,
		}

	def perform(self, command):
		return append_renewed_initial_reservation(command)

	def authorize_result(self, result):
		verify_existing_renewed_reservation(self.command, result)
