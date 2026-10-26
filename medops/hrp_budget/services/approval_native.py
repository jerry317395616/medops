from __future__ import annotations

import frappe

from medops.common.budget_workflow import (
	APPROVER_ROLE,
	CASE,
	STATES,
	WORKFLOW,
	WORKFLOW_ACTIONS,
	approval_mutation,
)
from medops.common.domain_service import fingerprint_json
from medops.services.errors import raise_ione_error

TODO_PREFIX = "hrp-budget-approval-"
APPROVAL_ROLES = ("System Manager", "HRP System Manager", APPROVER_ROLE)
WITHDRAW_ROLES = (*APPROVAL_ROLES, "HRP Budget User", "HRP Budget Manager", "HRP Finance User")


def workflow_states():
	return [
		dict(
			state=state,
			doc_status="1" if name == "Approved" else "0",
			allow_edit="System Manager",
			is_optional_state=1,
			send_email=0,
			update_field=None,
			update_value=None,
			evaluate_as_expression=0,
		)
		for name, state in STATES.items()
	]


def workflow_transitions():
	return [
		dict(
			state=STATES["Pending"],
			action=action,
			next_state=STATES[status],
			allowed=role,
			allow_self_approval=1,
			condition=None,
			transition_tasks=None,
			send_email_to_creator=0,
		)
		for status, action in WORKFLOW_ACTIONS.items()
		for role in (WITHDRAW_ROLES if status == "Withdrawn" else APPROVAL_ROLES)
	]


def validate_workflow_definition(doc):
	if (
		doc.name != WORKFLOW
		or doc.document_type != CASE
		or not doc.is_active
		or doc.workflow_state_field != "workflow_state"
		or doc.send_email_alert
		or doc.override_status
	):
		raise_ione_error("CONFIGURATION_INVALID")

	def projected(rows, expected):
		return sorted([tuple(str(row.get(key) or "") for key in expected[0]) for row in rows])

	if projected(doc.states, workflow_states()) != projected(
		workflow_states(), workflow_states()
	) or projected(doc.transitions, workflow_transitions()) != projected(
		workflow_transitions(), workflow_transitions()
	):
		raise_ione_error("CONFIGURATION_INVALID")


def require_workflow_integrity():
	# This adapter consumes a session-bound confirmation in its current transaction.
	# Background submission cannot carry its ContextVar capability safely.
	if frappe.get_meta(CASE).queue_in_background:
		raise_ione_error("CONFIGURATION_INVALID")
	names = frappe.get_all("Workflow", filters={"document_type": CASE, "is_active": 1}, pluck="name", limit=3)
	if names != [WORKFLOW]:
		raise_ione_error("CONFIGURATION_INVALID")
	mutation = approval_mutation()
	doc = frappe.get_doc("Workflow", WORKFLOW, for_update=bool(mutation))
	validate_workflow_definition(doc)
	return doc


def protect_workflow(doc, event=None, *args, **kwargs):
	del args, kwargs
	before = doc.get_doc_before_save()
	if doc.name != WORKFLOW and doc.document_type != CASE and not (before and before.document_type == CASE):
		return
	if event in {"on_trash", "before_rename"}:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	validate_workflow_definition(doc)


def protect_workflow_label(doc, event=None, *args, **kwargs):
	del event, args, kwargs
	if doc.name in {*STATES.values(), *WORKFLOW_ACTIONS.values()}:
		raise_ione_error("OPERATION_NOT_ALLOWED")


def sync_tasks(case, users: set[str]):
	mutation = approval_mutation()
	if not mutation or mutation.case != case.name:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	if case.status != "Pending":
		users = set()
	active_names = {
		TODO_PREFIX + fingerprint_json([case.name, int(case.current_step), user])[:40]: user for user in users
	}
	with_todos = []
	for row in frappe.get_all(
		"ToDo",
		filters={"reference_type": CASE, "reference_name": case.name, "status": "Open"},
		pluck="name",
		limit=1001,
	):
		if len(with_todos) >= 1000:
			raise_ione_error("CONFIGURATION_INVALID")
		with_todos.append(row)
		if row not in active_names:
			doc = frappe.get_doc("ToDo", row, for_update=True)
			doc.status = "Closed"
			doc.save(ignore_permissions=True)
	for name, user in active_names.items():
		if frappe.db.exists("ToDo", name):
			continue
		frappe.get_doc(
			dict(
				doctype="ToDo",
				allocated_to=user,
				assigned_by=frappe.session.user,
				reference_type=CASE,
				reference_name=case.name,
				status="Open",
				priority="High",
				date=str(case.posting_date),
				description="预算审批待办；请在预算审批页面核对事实并作出决定。",
			)
		).insert(ignore_permissions=True, set_name=name)


def protect_todo(doc, event=None):
	before = doc.get_doc_before_save()
	owned = (
		doc.reference_type == CASE
		or str(doc.name or "").startswith(TODO_PREFIX)
		or bool(before and (before.reference_type == CASE or str(before.name or "").startswith(TODO_PREFIX)))
	)
	if not owned:
		return
	mutation = approval_mutation()
	if mutation and doc.reference_type == CASE and doc.reference_name == mutation.case:
		return
	if event in {"on_trash", "before_rename"} or not before:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	from medops.hrp_budget.services.approval_access import get_case

	get_case(str(doc.reference_name))
	if (
		doc.allocated_to != frappe.session.user
		or before.status != "Open"
		or doc.status != "Closed"
		or any(
			before.get(field.fieldname) != doc.get(field.fieldname)
			for field in doc.meta.fields
			if field.fieldname != "status"
		)
	):
		raise_ione_error("OPERATION_NOT_ALLOWED")


def todo_query(user=None):
	del user
	# Owned pending work is listed only by the source-authorized case facade.
	return "COALESCE(`tabToDo`.`reference_type`,'') <> 'HRP Budget Approval'"


def todo_permission(doc, user=None, ptype=None, debug=False):
	del debug
	if doc.reference_type != CASE:
		return None
	from medops.hrp_budget.services.approval_access import case_permission

	if not doc.reference_name or not frappe.db.exists(CASE, doc.reference_name):
		return False
	return case_permission(
		frappe.get_doc(CASE, doc.reference_name), user=user, ptype="read"
	) and ptype not in {"create", "delete", "cancel", "submit", "amend"}
