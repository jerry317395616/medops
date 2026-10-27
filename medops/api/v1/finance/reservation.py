"""Experimental reservation confirmation evidence, disabled by default.

These endpoints do not reserve a budget or authorize financial posting.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import frappe

from medops.api.v1.finance.planning import request_guard
from medops.common.budget_planning import object_payload
from medops.common.budget_workflow import build_human_confirmation
from medops.common.material_source_snapshot import material_source_snapshot, parse_material_source_snapshot
from medops.common.reservation_request import build_reservation_prepare
from medops.hrp_budget.api.reservation_transaction import prepare_reservation_http_transaction
from medops.hrp_budget.services.approval import require_web_session
from medops.hrp_budget.services.material_request_source import locked_material_request_source
from medops.hrp_budget.services.planning import execution_payload
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.reservation_confirmation_service import (
	ConfirmReservationDecision,
	PrepareReservationConfirmation,
)
from medops.services.errors import raise_ione_error, require_roles


def _enabled():
	if frappe.conf.get("medops_reservation_confirmation_enabled") is not True:
		raise_ione_error("OPERATION_NOT_ALLOWED")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def source(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, MANAGER_ROLES, correlation_id):
		require_web_session()
		_enabled()
		data = object_payload(payload, {"request", "line", "organization_unit"})
		if set(data) != {"request", "line", "organization_unit"}:
			raise ValueError("complete material source reference required")
		prepare_reservation_http_transaction()
		item = locked_material_request_source(data["request"], data["line"], data["organization_unit"])
		snapshot, digest = material_source_snapshot(asdict(item))
		require_roles(MANAGER_ROLES)
		return {"source_snapshot": snapshot, "source_snapshot_digest": digest}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def prepare(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, MANAGER_ROLES, correlation_id):
		require_web_session()
		_enabled()
		item = build_reservation_prepare(payload)
		prepare_reservation_http_transaction()
		result = PrepareReservationConfirmation().execute(item, correlation_id=correlation_id)
		facts = parse_material_source_snapshot(
			item.source_snapshot, expected_digest=item.source_snapshot_digest
		)
		return {
			**execution_payload(result),
			"effect": "confirmation_only",
			"review": {
				"source": {
					key: facts[key]
					for key in (
						"request",
						"line",
						"item_code",
						"uom",
						"quantity",
						"amount",
						"cost_center",
						"expense_account",
					)
				},
				"allocation": item.allocation,
				"budget_key": asdict(item.key),
				"currency": item.currency,
				"amount": item.amount,
				"budget_date": item.budget_date,
				"reason": item.reason,
				"source_snapshot_digest": item.source_snapshot_digest,
			},
		}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def confirm(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, MANAGER_ROLES, correlation_id):
		require_web_session()
		_enabled()
		command = build_human_confirmation(payload)
		prepare_reservation_http_transaction()
		result = ConfirmReservationDecision().execute(command, correlation_id=correlation_id)
		return {**execution_payload(result), "effect": "confirmation_only"}
