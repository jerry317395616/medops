from __future__ import annotations

import frappe

from medops.hrp_foundation.services import get_performance_baseline_contract_status


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_performance_baseline_contract() -> dict[str, object]:
	"""Return the governed read-only performance contract."""
	return get_performance_baseline_contract_status()


__all__ = ["get_performance_baseline_contract"]
