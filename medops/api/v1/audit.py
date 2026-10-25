from __future__ import annotations

import frappe

from medops.services.audit_context import get_audit_context_status


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_audit_context() -> dict[str, object]:
	return get_audit_context_status()
