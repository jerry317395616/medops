from __future__ import annotations

import frappe

from medops.hrp_foundation.services import get_software_supply_chain_contract_status


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_software_supply_chain_contract() -> dict[str, object]:
	"""Return the governed read-only SBOM and security scan contract."""
	return get_software_supply_chain_contract_status()


__all__ = ["get_software_supply_chain_contract"]
