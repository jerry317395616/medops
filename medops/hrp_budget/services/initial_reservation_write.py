"""Private, default-disabled candidate for an initial reservation transaction."""

from __future__ import annotations

from dataclasses import asdict

import frappe
from frappe.utils import now_datetime

from medops.common.authorized_reservation_projection import verify_authorized_initial_reservation
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS
from medops.common.budget_reservation_storage import ROW_FIELDS
from medops.common.initial_reservation_posting import initial_reservation_documents
from medops.common.material_source_snapshot import parse_material_source_snapshot
from medops.common.reservation_authorization import (
	AUTHORIZATION_ROW_FIELDS,
	verify_reservation_authorization_rows,
	verify_reservation_confirmation_proof,
	verify_reservation_confirmation_row,
)
from medops.hrp_budget.services.approval import require_web_session, session_digest
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.initial_reservation_control import EVENT, ROOT, expected_initial_reservation
from medops.hrp_budget.services.material_budget_guards import assert_material_budget_guards
from medops.hrp_budget.services.material_request_source import locked_material_request_source
from medops.hrp_budget.services.release_documents import same_value
from medops.hrp_budget.services.reservation_confirmation import DOCTYPE, revalidate_reservation_intent
from medops.services.errors import raise_ione_error


def _evidence(name, *, lock=False):
	doc = frappe.get_doc(DOCTYPE, name, for_update=lock)
	return {field: doc.get(field) for field in AUTHORIZATION_ROW_FIELDS}


def _authorized(first, second, proof):
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
			now=now_datetime().isoformat(sep=" ", timespec="microseconds"),
		)
	except ValueError as error:
		raise_ione_error("PERMISSION_DENIED", cause=error)
	return item, decision, session


def _check_persisted_document(doc, expected, docstatus):
	if doc.name != expected["name"] or int(doc.docstatus) != docstatus:
		raise_ione_error("CONFLICT")
	for field in doc.meta.fields:
		if field.fieldtype in {"Section Break", "Column Break", "Tab Break"}:
			continue
		if not same_value(field, doc.get(field.fieldname), expected.get(field.fieldname)):
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
		if len(prior) > 100 or any(
			row.source_snapshot_digest != item.source_snapshot_digest for row in prior
		):
			raise_ione_error("CONFLICT")
		if frappe.db.get_value(
			ROOT, {"source_identity_key": item.reservation_identity}, "name", for_update=True
		):
			raise_ione_error("CONFLICT")
		documents = initial_reservation_documents(
			locked_first,
			locked_second,
			occurred_at=now_datetime().isoformat(sep=" ", timespec="microseconds"),
		)
		with expected_initial_reservation(
			documents, source, (item.key,), confirmation=confirmation, decision=decision, proof=proof
		):
			root = frappe.get_doc(dict(doctype=ROOT, **documents.root)).insert(ignore_permissions=True)
			root.submit()
			event = frappe.get_doc(dict(doctype=EVENT, **documents.event)).insert(ignore_permissions=True)
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
		verify_authorized_initial_reservation(root_row, [event_row], final_first, final_second, current_facts=asdict(source))
		revalidate_reservation_intent(item, actor=actor, session=session)
		assert_material_budget_guards(source, [item.key])
		return {"reservation": root.name, "event": event.name, "intent_digest": item.intent_digest}
	except BaseException:
		invalidate_budget_guards()
		raise
