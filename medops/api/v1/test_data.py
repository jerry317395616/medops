from __future__ import annotations

import frappe

from medops.hrp_foundation.services import get_test_data_factory_contract_status


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_test_data_factory_contract() -> dict[str, object]:
	"""Return the read-only scenario contract; generation remains service-only."""
	return get_test_data_factory_contract_status()


__all__ = ["get_test_data_factory_contract"]
