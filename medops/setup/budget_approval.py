from __future__ import annotations

import frappe

from medops.common.budget_workflow import APPROVER_ROLE, CASE, REVIEW, STATES, WORKFLOW, WORKFLOW_ACTIONS
from medops.hrp_budget.services.approval_native import (
	require_workflow_integrity,
	workflow_states,
	workflow_transitions,
)
from medops.services.errors import raise_ione_error


def ensure_budget_approval():
	if not frappe.db.exists("Role", APPROVER_ROLE):
		frappe.get_doc(dict(doctype="Role", role_name=APPROVER_ROLE, desk_access=1, is_custom=0)).insert(
			ignore_permissions=True
		)
	for name in STATES.values():
		if not frappe.db.exists("Workflow State", name):
			frappe.get_doc(dict(doctype="Workflow State", workflow_state_name=name)).insert(
				ignore_permissions=True
			)
	for name in WORKFLOW_ACTIONS.values():
		if not frappe.db.exists("Workflow Action Master", name):
			frappe.get_doc(dict(doctype="Workflow Action Master", workflow_action_name=name)).insert(
				ignore_permissions=True
			)
	if frappe.db.exists("Workflow", WORKFLOW):
		require_workflow_integrity()
	else:
		if frappe.db.exists("Workflow", {"document_type": CASE}):
			raise_ione_error("CONFIGURATION_INVALID")
		frappe.get_doc(
			dict(
				doctype="Workflow",
				workflow_name=WORKFLOW,
				document_type=CASE,
				is_active=1,
				workflow_state_field="workflow_state",
				override_status=0,
				send_email_alert=0,
				states=workflow_states(),
				transitions=workflow_transitions(),
			)
		).insert(ignore_permissions=True)
	for doctype, fields, name in (
		(CASE, ["submission", "round_number"], "uniq_hrp_budget_approval_round"),
		(CASE, ["company", "hospital", "organization_unit", "status"], "idx_hrp_budget_approval_scope"),
		(REVIEW, ["case", "event_kind", "case_revision"], "idx_hrp_budget_review_history"),
		(REVIEW, ["actor", "event_kind", "expires_at"], "idx_hrp_budget_review_actor"),
	):
		if name.startswith("uniq_"):
			frappe.db.add_unique(doctype, fields, name)
		else:
			# The fixed field `case` is a SQL keyword; this pinned MariaDB helper
			# interpolates field identifiers without quoting them itself.
			frappe.db.add_index(doctype, [f"`{field}`" for field in fields], name)
	return {"schema_version": 1}
