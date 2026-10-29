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
from medops.common.budget_planning import FUND, VALUE, object_payload
from medops.common.material_source_snapshot import parse_material_source_snapshot
from medops.common.reservation_commitment_request import (
	build_commitment_orders_query,
	build_commitment_post,
	build_commitment_prepare,
)
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
from medops.hrp_budget.services.planning import execution_payload, native_document
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.purchase_order_source import locked_linked_purchase_orders
from medops.hrp_budget.services.release_execution import refresh_financial_authority
from medops.hrp_budget.services.renewed_initial_reservation_write import PostRenewedInitialReservation
from medops.hrp_budget.services.reservation_commitment_confirmation import (
	check_commitment_confirmation_proof,
	read_commitment_confirmation,
)
from medops.hrp_budget.services.reservation_commitment_confirmation import enabled as commitment_enabled
from medops.hrp_budget.services.reservation_commitment_state import recheck_reservation_commitment
from medops.hrp_budget.services.reservation_commitment_write import (
	PostReservationCommitment,
	PrepareReservationCommitment,
)
from medops.hrp_budget.services.reservation_recovery import ReadReservationRecovery
from medops.hrp_budget.services.reservation_release_state import (
	inspect_reservation_release,
	read_reservation_original,
	recheck_reservation_release,
)
from medops.hrp_budget.services.reservation_release_write import (
	PostReservationRelease,
	PrepareReservationRelease,
)
from medops.hrp_budget.services.reservation_renewal_write import RenewReservationDecision
from medops.services.audit_context import emit_audit_event
from medops.services.errors import raise_ione_error, require_roles


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


def _order_review(order):
	# The readable items table already supplies its native row order; no new
	# financial field or client-provided ordinal determines the stable line ID.
	doc = native_document("Purchase Order", order.order, {"items.item_code"})
	positions = [index for index, row in enumerate(doc.items, 1) if row.name == order.line]
	if len(positions) != 1:
		raise_ione_error("CONFLICT")
	return {
		**{
			key: getattr(order, key)
			for key in (
				"order",
				"line",
				"supplier",
				"transaction_date",
				"item_code",
				"uom",
				"quantity",
				"amount",
				"currency",
			)
		},
		"line_number": positions[0],
	}


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
			"source_digest": state.source.digest(),
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
def commitment_orders(payload: Any = None, correlation_id: Any = None, **extra):
	"""Read bounded native linked lines before any budget guard; never quote available funds."""
	with _request(extra, correlation_id):
		name, digest = build_commitment_orders_query(payload)
		commitment_enabled()
		prepare_reservation_http_transaction()
		actor = frappe.session.user
		refresh_financial_authority(actor)
		require_roles(MANAGER_ROLES)
		_, original, first, second = read_reservation_original(name)
		facts = parse_material_source_snapshot(
			original.source_snapshot, expected_digest=original.source_snapshot_digest
		)
		source, orders = locked_linked_purchase_orders(
			facts["request"],
			facts["line"],
			original.key.organization_unit,
			expected_request_digest=digest,
		)
		rows = [_order_review(order) for order in orders]
		_, checked, checked_first, checked_second = read_reservation_original(name)
		refresh_financial_authority(actor)
		require_roles(MANAGER_ROLES)
		if (checked, checked_first, checked_second) != (
			original,
			first,
			second,
		) or frappe.session.user != actor:
			raise_ione_error("CONFLICT")
		emit_audit_event("budget_commitment_orders_inspected", reservation=name, order_count=len(orders))
		return {
			"reservation": name,
			"source_digest": source.digest(),
			"effect": "read_only",
			"orders": rows,
		}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def prepare_commitment(payload: Any = None, correlation_id: Any = None, **extra):
	"""Persist independent preview and display current authorized native review facts."""
	with _request(extra, correlation_id):
		command = build_commitment_prepare(payload)
		service = PrepareReservationCommitment()
		result = _execute(service, command, correlation_id, "confirmation_only")
		if service.state is None:
			raise_ione_error("CONFLICT")
		state = recheck_reservation_commitment(service.state)
		subject = native_document(VALUE, state.item.key.budget_subject, {"code", "display_name"})
		fund = native_document(FUND, state.item.key.funding_source, {"code", "display_name"})
		item, confirmed, _ = read_commitment_confirmation(result["confirmation"], lock=True)
		if item != state.item:
			raise_ione_error("CONFLICT")
		review = {
			"actor": state.actor,
			"source": {
				key: getattr(state.inspection.source, key)
				for key in (
					"request",
					"line",
					"item_code",
					"amount",
					"currency",
					"cost_center",
					"expense_account",
				)
			},
			"order": _order_review(state.purchase),
			"budget_key": asdict(state.item.key),
			"budget_subject_label": f"{subject.display_name} ({subject.code})",
			"funding_source_label": f"{fund.display_name} ({fund.code})",
			"currency": state.item.currency,
			"precision": state.item.precision,
			"amount": state.item.amount,
			"reason": state.item.reason,
			"open_before": state.item.open_before,
			"order_remaining": state.capacity.remaining,
			"budget_date": state.inspection.budget_date,
		}
		recheck_reservation_commitment(state)
		check_commitment_confirmation_proof(state, confirmed, result["proof"])
		return {**result, "review": review}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def post_commitment(payload: Any = None, correlation_id: Any = None, **extra):
	with _request(extra, correlation_id):
		return _execute(
			PostReservationCommitment(),
			build_commitment_post(payload),
			correlation_id,
			"reservation_committed",
		)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def recovery(payload: Any = None, correlation_id: Any = None, **extra):
	"""Recover current decision/result references; never issue old or new proofs."""
	with _request(extra, correlation_id):
		command = build_reservation_recovery(payload)
		prepare_reservation_http_transaction()
		result = ReadReservationRecovery().execute(command, correlation_id=correlation_id)
		return execution_payload(result)
