from __future__ import annotations

import frappe

from medops.services.environment import get_environment_status
from medops.services.errors import require_authenticated_user
from medops.setup.versions import get_runtime_versions, get_version_status


@frappe.whitelist(methods=["GET"])
def get_health() -> dict[str, object]:
	require_authenticated_user()
	return {
		"status": "ok",
		"site": frappe.local.site,
		"user": frappe.session.user,
		"environment": get_environment_status(),
		"versions": get_runtime_versions(),
		"installed_apps": frappe.get_installed_apps(),
	}


@frappe.whitelist(methods=["GET"])
def get_upstream_version_status() -> dict[str, object]:
	require_authenticated_user()
	return get_version_status()
