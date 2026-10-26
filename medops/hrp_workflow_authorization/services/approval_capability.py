from __future__ import annotations

import frappe

from medops.common.budget_workflow import CASE
from medops.hrp_workflow_authorization.permissions import has_scoped_permission


def can_approve_target(user: str, doc) -> bool:
	"""Owned financial cases have a fixed service capability, never native write."""
	if doc.doctype == CASE:
		from medops.hrp_budget.services.approval_access import can_approve_case

		return can_approve_case(user, doc)
	return bool(
		frappe.has_permission(doc.doctype, ptype="write", doc=doc, user=user)
		and has_scoped_permission(doc, user=user, ptype="write")
	)


def can_delegate_target_scope(user: str, context) -> bool:
	if context.doctype == CASE:
		from medops.hrp_budget.services.approval_access import approver_role_active

		return bool(approver_role_active(user) and has_scoped_permission(context, user=user, ptype="write"))
	return bool(
		frappe.has_permission(context.doctype, ptype="write", user=user)
		and has_scoped_permission(context, user=user, ptype="write")
	)
