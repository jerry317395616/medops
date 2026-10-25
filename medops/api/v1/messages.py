from __future__ import annotations

import frappe

from medops.common.transactional_message import TransactionalMessagePublicContract
from medops.services.transactional_message import (
	get_transactional_message_contract_status,
)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_transactional_message_contract() -> TransactionalMessagePublicContract:
	return get_transactional_message_contract_status()


__all__ = ["get_transactional_message_contract"]
