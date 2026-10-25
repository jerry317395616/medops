from __future__ import annotations

import frappe

from medops.common.access_scope import ScopeDecision
from medops.hrp_workflow_authorization.services.access_scope import resolve_access_scope
from medops.services.audit_context import service_audit_scope


@frappe.whitelist(allow_guest=True, methods=["POST"])
def resolve(
	doctype: str,
	action: str = "read",
	user: str | None = None,
	correlation_id: str | None = None,
) -> ScopeDecision:
	with service_audit_scope(correlation_id):
		return resolve_access_scope(user=user, doctype=doctype, action=action)


__all__ = ["resolve"]
