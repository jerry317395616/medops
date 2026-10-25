from __future__ import annotations

import frappe

from medops.services.errors import get_error_catalog_status as get_status


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_error_catalog() -> dict[str, object]:
	return get_status()
