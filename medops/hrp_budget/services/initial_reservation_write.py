"""Private, default-disabled candidate for an initial reservation transaction."""

from __future__ import annotations

import hashlib
from dataclasses import asdict

import frappe
from frappe.utils import now_datetime

from medops.common.authorized_reservation_projection import verify_authorized_initial_reservation
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS
from medops.common.budget_reservation_storage import ROW_FIELDS, reservation_record_from_row
from medops.common.domain_service import DomainServiceDefinition, normalize_sha256
from medops.common.initial_reservation_posting import initial_reservation_documents
from medops.common.material_source_snapshot import parse_material_source_snapshot
from medops.common.reservation_authorization import (
	AUTHORIZATION_ROW_FIELDS,
	verify_reservation_authorization_rows,
	verify_reservation_confirmation_proof,
	verify_reservation_confirmation_row,
)
from medops.common.reservation_request import ReservationPostCommand, build_reservation_post
from medops.hrp_budget.services.approval import require_web_session, session_digest
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.initial_reservation_control import EVENT, ROOT, expected_initial_reservation
from medops.hrp_budget.services.material_budget_guards import assert_material_budget_guards
from medops.hrp_budget.services.material_request_source import locked_material_request_source
from medops.hrp_budget.services.material_usage_preview import inspect_existing_purchase_usage
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.release_documents import same_value
from medops.hrp_budget.services.reservation_confirmation import (
	DOCTYPE,
	require_reservation_actor_scope,
	revalidate_reservation_intent,
)
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error


def _evidence(name, *, lock=False):
	doc = frappe.get_doc(DOCTYPE, name, for_update=lock)
	return {field: doc.get(field) for field in AUTHORIZATION_ROW_FIELDS}


def _authorized(first, second, proof, *, replay=False):
	item, confirmed = verify_reservation_confirmation_row(first)
	checked, decision = verify_reservation_authorization_rows(first, second)
	if checked != item or decision.actor != frappe.session.user:
		raise_ione_error("PERMISSION_DENIED")
	session = session_digest("budget-reservation:" + item.reservation_identity)
	try:
		verify_reservation_confirmation_proof(
			item,
			confirmed,
			proof=proof,
			actor=frappe.session.user,
			session_digest=session,
			now=decision.recorded_at
			if replay
			else now_datetime().isoformat(sep=" ", timespec="microseconds"),
		)
	except ValueError as error:
		raise_ione_error("PERMISSION_DENIED", cause=error)
	return item, decision, session


def verify_existing_initial_reservation(command: ReservationPostCommand, result: dict[str, object]) -> None:
	"""Revalidate an actual posted result for the current actor and session.

	Replay checks the recorded decision time rather than extending its validity to
	a new write. The existing root, single event, source and current permissions
	must be independently checked under the shared guard.
	"""
	try:
		if frappe.conf.get("medops_internal_reservation_write_enabled") is not True:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		require_web_session()
		if set(result) != {"reservation", "event", "intent_digest"}:
			raise ValueError("complete reservation result required")
		for field in ("reservation", "event", "intent_digest"):
			normalize_sha256(result[field], label=field)
		first, second = _evidence(command.confirmation), _evidence(command.decision)
		item, decision, session = _authorized(first, second, command.proof, replay=True)
		actor = frappe.session.user
		require_reservation_actor_scope(item, actor, session)
		facts = parse_material_source_snapshot(
			item.source_snapshot, expected_digest=item.source_snapshot_digest
		)
		if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is None:
			inspection = inspect_existing_purchase_usage(
				facts["request"],
				facts["line"],
				item.key.organization_unit,
				budget_date=item.budget_date,
				proposed_keys=[asdict(item.key)],
			)
			source = inspection.source
			if not any(position.reservation == result["reservation"] for position in inspection.source_usage):
				raise ValueError("posted reservation is not in the checked source usage")
		else:
			source = locked_material_request_source(
				facts["request"], facts["line"], item.key.organization_unit
			)
			assert_material_budget_guards(source, [item.key])
		locked_first, locked_second = (
			_evidence(command.confirmation, lock=True),
			_evidence(command.decision, lock=True),
		)
		locked_item, locked_decision, locked_session = _authorized(
			locked_first, locked_second, command.proof, replay=True
		)
		if (locked_item, locked_decision, locked_session) != (item, decision, session):
			raise ValueError("reservation authorization changed")
		root = frappe.get_doc(ROOT, result["reservation"], for_update=True)
		names = frappe.get_all(
			EVENT, filters={"reservation": root.name}, pluck="name", order_by="name asc", limit_page_length=2
		)
		if names != [result["event"]]:
			raise ValueError("single initial reservation event required")
		event = frappe.get_doc(EVENT, names[0], for_update=True)
		event_row = {field: event.get(field) for field in ROW_FIELDS}
		record = reservation_record_from_row(event_row)
		documents = initial_reservation_documents(locked_first, locked_second, occurred_at=record.occurred_at)
		_check_persisted_document(root, documents.root, 1)
		_check_persisted_document(event, documents.event, 0)
		root_row = {field: root.get(field) for field in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}}
		position, _, checked = verify_authorized_initial_reservation(
			root_row, [event_row], locked_first, locked_second, current_facts=asdict(source)
		)
		if (
			result["intent_digest"] != item.intent_digest
			or checked != decision
			or position.open_reservation != item.amount
		):
			raise ValueError("posted reservation differs from decision")
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


def _check_persisted_document(doc, expected, docstatus):
	if doc.name != expected["name"] or int(doc.docstatus) != docstatus:
		raise_ione_error("CONFLICT")
	for field in doc.meta.fields:
		if field.fieldtype in {"Section Break", "Column Break", "Tab Break"}:
			continue
		if not same_value(field, doc.get(field.fieldname), expected.get(field.fieldname)):
			raise_ione_error("CONFLICT")


def _exact_new_document(doctype, values):
	"""Prevent Frappe user/select defaults from inventing unstated business facts."""
	doc = frappe.get_doc(dict(doctype=doctype, **values))
	doc.dont_update_if_missing = [
		field.fieldname
		for field in doc.meta.fields
		if field.fieldname and values.get(field.fieldname) is None
	]
	return doc


def check_persisted_document(doc, expected, docstatus):
	"""Share exact persisted-document comparison with private budget transactions."""
	return _check_persisted_document(doc, expected, docstatus)


def exact_new_document(doctype, values):
	"""Share default-suppressed construction with private budget transactions."""
	return _exact_new_document(doctype, values)


def require_initial_identity_available(item, source):
	"""Reject duplicate identities and unsupported mixed source revisions under guard."""
	assert_material_budget_guards(source, [item.key])
	# The current reader supports only one historical revision per source line.
	# Do not create a second revision until lifecycle adapters support it.
	prior = frappe.get_all(
		ROOT,
		filters={
			"source_doctype": "Material Request",
			"source_name": source.request,
			"source_line": source.line,
		},
		fields=["name", "source_snapshot_digest"],
		limit_page_length=101,
	)
	if len(prior) > 100 or any(row.source_snapshot_digest != item.source_snapshot_digest for row in prior):
		raise_ione_error("CONFLICT")
	if frappe.db.get_value(ROOT, {"source_identity_key": item.reservation_identity}, "name", for_update=True):
		raise_ione_error("CONFLICT")


def append_initial_reservation(confirmation: str, decision: str, proof: str) -> dict[str, str]:
	"""Append one initial reservation in an owned, clean transaction.

	This private candidate has no HTTP entry point and is disabled by default.
	The caller must provide whole-transaction rollback, audit, idempotency and
	retry handling; a duplicate is rejected, not replayed. All writers must use
	the same fence before this may be enabled in a hospital.
	"""
	try:
		if frappe.conf.get("medops_internal_reservation_write_enabled") is not True:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		require_web_session()
		if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is not None:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		first, second = _evidence(confirmation), _evidence(decision)
		item, _, session = _authorized(first, second, proof)
		actor = frappe.session.user
		revalidate_reservation_intent(item, actor=actor, session=session)
		locked_first, locked_second = _evidence(confirmation, lock=True), _evidence(decision, lock=True)
		locked_item, _, checked_session = _authorized(locked_first, locked_second, proof)
		if locked_item != item or checked_session != session:
			raise_ione_error("CONFLICT")
		facts = parse_material_source_snapshot(
			item.source_snapshot, expected_digest=item.source_snapshot_digest
		)
		source = locked_material_request_source(facts["request"], facts["line"], item.key.organization_unit)
		assert_material_budget_guards(source, [item.key])
		require_initial_identity_available(item, source)
		documents = initial_reservation_documents(
			locked_first,
			locked_second,
			occurred_at=now_datetime().isoformat(sep=" ", timespec="microseconds"),
		)
		with expected_initial_reservation(
			documents, source, (item.key,), confirmation=confirmation, decision=decision, proof=proof
		):
			root = _exact_new_document(ROOT, documents.root).insert(ignore_permissions=True)
			root.submit()
			event = _exact_new_document(EVENT, documents.event).insert(ignore_permissions=True)
		root = frappe.get_doc(ROOT, root.name, for_update=True)
		event = frappe.get_doc(EVENT, event.name, for_update=True)
		_check_persisted_document(root, documents.root, 1)
		_check_persisted_document(event, documents.event, 0)
		root_row = {field: root.get(field) for field in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}}
		event_row = {field: event.get(field) for field in ROW_FIELDS}
		final_first, final_second = _evidence(confirmation, lock=True), _evidence(decision, lock=True)
		final_item, _, final_session = _authorized(final_first, final_second, proof)
		if final_item != item or final_session != session:
			raise_ione_error("CONFLICT")
		verify_authorized_initial_reservation(
			root_row, [event_row], final_first, final_second, current_facts=asdict(source)
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


class PostInitialReservation(DomainService[ReservationPostCommand]):
	"""Audited private command with a replay-checked encrypted receipt."""

	definition = DomainServiceDefinition(
		name="hrp_budget.reservation.post", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		require_web_session()
		if frappe.conf.get("medops_internal_reservation_write_enabled") is not True:
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def validate(self, command):
		try:
			if (
				type(command) is not ReservationPostCommand
				or build_reservation_post(
					{
						"confirmation": command.confirmation,
						"decision": command.decision,
						"proof": command.proof,
						"acknowledge": command.acknowledge,
					}
				)
				!= command
			):
				raise ValueError("canonical reservation post command required")
		except ValueError as error:
			raise_ione_error("INVALID_REQUEST", cause=error)
		self.command = command

	def request_payload(self, command):
		return {
			"confirmation": command.confirmation,
			"decision": command.decision,
			"proof_hash": hashlib.sha256(command.proof.encode()).hexdigest(),
			"acknowledge": True,
		}

	def perform(self, command):
		return append_initial_reservation(command.confirmation, command.decision, command.proof)

	def authorize_result(self, result):
		verify_existing_initial_reservation(self.command, result)
