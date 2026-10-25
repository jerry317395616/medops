from __future__ import annotations

import frappe

from medops.common.immutable_ledger import ImmutableLedgerPublicContract
from medops.services.immutable_ledger import get_immutable_ledger_contract_status


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_immutable_ledger_contract() -> ImmutableLedgerPublicContract:
	"""Return the read-only platform contract; ledger writes remain domain-service only."""
	return get_immutable_ledger_contract_status()


__all__ = ["get_immutable_ledger_contract"]
