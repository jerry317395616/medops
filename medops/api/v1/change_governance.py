from __future__ import annotations

import frappe

from medops.services.change_governance import get_change_governance_status as get_status


@frappe.whitelist(methods=["GET"])
def get_change_governance_status(correlation_id: str | None = None) -> dict[str, object]:
	return get_status(correlation_id)
