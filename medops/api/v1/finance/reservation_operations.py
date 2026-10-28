"""Human reservation operations, closed unless explicitly enabled for a web session.

The existing domain services own authorization, independent confirmation,
shared guards, audit and encrypted idempotency. Frappe owns the outer commit.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from typing import Any

import frappe

from medops.api.v1.finance.planning import request_guard
from medops.common.budget_planning import object_payload
from medops.common.reservation_recovery import build_reservation_recovery
from medops.common.reservation_release_request import (
	build_release_reservation_post,
	build_release_reservation_prepare,
)
from medops.common.reservation_renewal_request import (
	build_renewal_command,
	build_renewed_reservation_post,
)
from medops.common.reservation_request import build_reservation_post
from medops.hrp_budget.api.reservation_transaction import prepare_reservation_http_transaction
from medops.hrp_budget.services.approval import require_web_session
from medops.hrp_budget.services.initial_reservation_write import PostInitialReservation
from medops.hrp_budget.services.planning import execution_payload
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.renewed_initial_reservation_write import PostRenewedInitialReservation
from medops.hrp_budget.services.reservation_recovery import ReadReservationRecovery
from medops.hrp_budget.services.reservation_release_state import (
	inspect_reservation_release,
	recheck_reservation_release,
)
from medops.hrp_budget.services.reservation_release_write import (
	PostReservationRelease,
	PrepareReservationRelease,
)
from medops.hrp_budget.services.reservation_renewal_write import RenewReservationDecision
from medops.services.audit_context import emit_audit_event
from medops.services.errors import raise_ione_error


def _enabled():
	if frappe.conf.get("medops_reservation_operations_enabled") is not True:
		raise_ione_error("OPERATION_NOT_ALLOWED")


@contextmanager
def _request(extra, correlation_id):
	with request_guard(extra, MANAGER_ROLES, correlation_id):
		require_web_session()
		_enabled()
		yield


def _execute(service, command, correlation_id, effect):
	prepare_reservation_http_transaction()
	result = service.execute(command, correlation_id=correlation_id)
	return {**execution_payload(result), "effect": effect}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def post(payload: Any = None, correlation_id: Any = None, **extra):
	with _request(extra, correlation_id):
		return _execute(
			PostInitialReservation(), build_reservation_post(payload), correlation_id, "reservation_posted"
		)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def renew(payload: Any = None, correlation_id: Any = None, **extra):
	with _request(extra, correlation_id):
		return _execute(
			RenewReservationDecision(), build_renewal_command(payload), correlation_id, "decision_renewed"
		)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def post_renewed(payload: Any = None, correlation_id: Any = None, **extra):
	with _request(extra, correlation_id):
		return _execute(
			PostRenewedInitialReservation(),
			build_renewed_reservation_post(payload),
			correlation_id,
			"reservation_posted",
		)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def prepare_release(payload: Any = None, correlation_id: Any = None, **extra):
	with _request(extra, correlation_id):
		command = build_release_reservation_prepare(payload)
		result = _execute(PrepareReservationRelease(), command, correlation_id, "confirmation_only")
		# Exact normalized reason; the domain receipt binds its hash and the full
		# persisted release intent. Never return proof/session hashes or private JSON.
		return {**result, "reason": command.reason}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def post_release(payload: Any = None, correlation_id: Any = None, **extra):
	with _request(extra, correlation_id):
		return _execute(
			PostReservationRelease(),
			build_release_reservation_post(payload),
			correlation_id,
			"reservation_released",
		)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def inspect(payload: Any = None, correlation_id: Any = None, **extra):
	"""Display a complete currently authorized position; never authorize a write."""
	with _request(extra, correlation_id):
		data = object_payload(payload, {"reservation"})
		if set(data) != {"reservation"}:
			raise ValueError("one reservation reference required")
		prepare_reservation_http_transaction()
		state = inspect_reservation_release(data["reservation"])
		result = {
			"position": asdict(state.position),
			"source": {
				key: getattr(state.source, key)
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
			"allocation": state.original.allocation,
			"budget_date": state.original.budget_date,
			"effect": "read_only",
		}
		emit_audit_event(
			"budget_reservation_inspected",
			reservation=state.position.reservation,
			sequence=state.position.last_sequence,
		)
		recheck_reservation_release(state)
		return result


@frappe.whitelist(allow_guest=True, methods=["POST"])
def recovery(payload: Any = None, correlation_id: Any = None, **extra):
	"""Recover current decision/result references; never issue old or new proofs."""
	with _request(extra, correlation_id):
		command = build_reservation_recovery(payload)
		prepare_reservation_http_transaction()
		result = ReadReservationRecovery().execute(command, correlation_id=correlation_id)
		return execution_payload(result)
