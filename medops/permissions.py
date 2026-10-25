from __future__ import annotations

import frappe

_ALLOWED_APP_ROLES = {
	"System Manager",
	"HRP System Manager",
	"HRP User",
	"HRP Department Manager",
	"HRP Auditor",
	"HRP Integration User",
}


def check_app_permission() -> bool:
	"""Show the app to authenticated users who have an HRP or System Manager role."""
	if frappe.session.user == "Guest":
		return False
	return bool(_ALLOWED_APP_ROLES.intersection(frappe.get_roles()))
