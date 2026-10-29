"""Audited, idempotent private commitment preview and atomic human-authorized posting."""

from __future__ import annotations

import hashlib
from dataclasses import asdict

import frappe

from medops.common.commitment_evidence import (
	COMMITMENT_ROW_FIELDS,
	commitment_evidence_row,
	verify_commitment_evidence_row,
)
from medops.common.domain_service import DomainServiceDefinition, normalize_sha256
from medops.common.reservation_commitment_history import commitment_record
from medops.common.reservation_commitment_request import (
	CommitmentPost,
	CommitmentPrepare,
	build_commitment_post,
	build_commitment_prepare,
)
from medops.hrp_budget.services import reservation_commitment_confirmation as confirmation
from medops.hrp_budget.services.approval import require_web_session
from medops.hrp_budget.services.commitment_authorization import DOCTYPE
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.initial_reservation_write import check_persisted_document, exact_new_document
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.reservation_commitment_allocations import (
	read_guarded_commitment_records,
	verify_guarded_purchase_allocations,
)
from medops.hrp_budget.services.reservation_commitment_control import EVENT, enabled, expected_commitment_post
from medops.hrp_budget.services.reservation_commitment_state import (
	LockedCommitmentState,
	check_commitment_state_access,
	commitment_session,
	inspect_commitment_history,
	inspect_reservation_commitment,
	recheck_reservation_commitment,
)
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error

PREPARE_FIELDS = {
	"confirmation",
	"proof",
	"expires_at",
	"intent_digest",
	"reservation",
	"purchase_order",
	"purchase_order_line",
	"amount",
	"currency",
	"precision",
}
POST_FIELDS = {"reservation", "commitment_decision", "event", "intent_digest"}


def poison(error):
	invalidate_budget_guards()
	if isinstance(error, (ValueError, TypeError, KeyError)):
		raise_ione_error("CONFLICT", cause=error)
	raise error


def read_confirmation(name):
	item, confirmed, doc = confirmation.read_commitment_confirmation(name)
	check_persisted_document(
		doc,
		commitment_evidence_row(
			item,
			confirmed,
			material_snapshot=doc.material_snapshot_json,
			order_snapshot=doc.order_snapshot_json,
		),
		0,
	)
	if doc.owner != confirmed.actor:
		raise ValueError("confirmation owner differs from its actor")
	return item, confirmed, doc


class PrepareReservationCommitment(DomainService[CommitmentPrepare]):
	definition = DomainServiceDefinition(
		name="hrp_budget.reservation_commitment.preview",
		version=1,
		kind="command",
		required_roles=MANAGER_ROLES,
	)

	def authorize(self, command):
		super().authorize(command)
		confirmation.enabled()
		require_web_session()

	def validate(self, command):
		try:
			if type(command) is not CommitmentPrepare or build_commitment_prepare(asdict(command)) != command:
				raise ValueError("canonical commitment preview command required")
		except (TypeError, ValueError, KeyError) as error:
			raise_ione_error("INVALID_REQUEST", cause=error)
		self.command = command
		self.state = None

	def request_payload(self, command):
		return {
			"reservation": command.reservation,
			"purchase_order": command.purchase_order,
			"purchase_order_line": command.purchase_order_line,
			"amount": command.amount,
			"reason_hash": hashlib.sha256(command.reason.encode()).hexdigest(),
		}

	def perform(self, command):
		try:
			self.state = inspect_reservation_commitment(
				command.reservation,
				command.purchase_order,
				command.purchase_order_line,
				amount=command.amount,
				reason=command.reason,
			)
			return confirmation.persist_prepared_commitment_confirmation(self.state)
		except BaseException as error:
			poison(error)

	def authorize_result(self, result):
		try:
			confirmation.enabled()
			if set(result) != PREPARE_FIELDS:
				raise ValueError("complete commitment preview result required")
			item, confirmed, _ = read_confirmation(result["confirmation"])
			if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is None:
				self.state = inspect_reservation_commitment(
					self.command.reservation,
					self.command.purchase_order,
					self.command.purchase_order_line,
					amount=self.command.amount,
					reason=self.command.reason,
				)
			elif self.state is None:
				raise_ione_error("OPERATION_NOT_ALLOWED")
			state = recheck_reservation_commitment(self.state)
			if (
				item != state.item
				or result["expires_at"] != confirmed.expires_at
				or any(
					result[key] != getattr(item, key)
					for key in ("reservation", "amount", "currency", "precision", "intent_digest")
				)
				or (result["purchase_order"], result["purchase_order_line"])
				!= (state.purchase.order, state.purchase.line)
			):
				raise ValueError("commitment preview differs from complete current intent")
			confirmation.check_commitment_confirmation_proof(state, confirmed, result["proof"])
		except BaseException as error:
			poison(error)


class PostReservationCommitment(DomainService[CommitmentPost]):
	definition = DomainServiceDefinition(
		name="hrp_budget.reservation_commitment.post", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		enabled()
		require_web_session()

	def validate(self, command):
		try:
			if type(command) is not CommitmentPost or build_commitment_post(asdict(command)) != command:
				raise ValueError("canonical commitment post command required")
		except (TypeError, ValueError, KeyError) as error:
			raise_ione_error("INVALID_REQUEST", cause=error)
		self.command = command
		self.inspection = None
		self.purchase = None

	def request_payload(self, command):
		return {
			"confirmation": command.confirmation,
			"proof_hash": hashlib.sha256(command.proof.encode()).hexdigest(),
			"acknowledge": True,
		}

	def perform(self, command):
		try:
			item, confirmed, doc = read_confirmation(command.confirmation)
			state = inspect_reservation_commitment(
				item.reservation,
				doc.purchase_order,
				doc.purchase_order_line,
				amount=item.amount,
				reason=item.reason,
			)
			if state.item != item:
				raise ValueError("confirmation no longer matches the current predecessor and sources")
			with expected_commitment_post(
				state, confirmed, command.proof, acknowledge=command.acknowledge
			) as write:
				exact_new_document(DOCTYPE, write.values).insert(ignore_permissions=True)
				exact_new_document(EVENT, write.event_values).insert(ignore_permissions=True)
			self.inspection, self.purchase = write.posted_inspection, state.purchase
			return {
				"reservation": item.reservation,
				"commitment_decision": write.decision.name,
				"event": write.record.event.name,
				"intent_digest": item.intent_digest,
			}
		except BaseException as error:
			poison(error)

	def authorize_result(self, result):
		try:
			enabled()
			if set(result) != POST_FIELDS:
				raise ValueError("complete commitment receipt required")
			for key in result:
				normalize_sha256(result[key], label=key)
			item, confirmed, first = read_confirmation(self.command.confirmation)
			second = frappe.get_doc(DOCTYPE, result["commitment_decision"], for_update=True)
			checked, decision = verify_commitment_evidence_row(
				{key: second.get(key) for key in COMMITMENT_ROW_FIELDS}, confirmed
			)
			check_persisted_document(
				second,
				commitment_evidence_row(
					item,
					decision,
					material_snapshot=first.material_snapshot_json,
					order_snapshot=first.order_snapshot_json,
					confirmed=confirmed,
				),
				0,
			)
			if (
				checked != item
				or result["reservation"] != item.reservation
				or result["intent_digest"] != item.intent_digest
			):
				raise ValueError("commitment receipt identity differs from independent evidence")
			if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is None:
				_, _, self.inspection, self.purchase = inspect_commitment_history(
					item.reservation, first.purchase_order, first.purchase_order_line
				)
			elif self.inspection is None or self.purchase is None:
				raise_ione_error("OPERATION_NOT_ALLOWED")
			capacity, pairs = verify_guarded_purchase_allocations(self.inspection, self.purchase)
			if len([pair for pair in pairs if pair[:3] == (item, confirmed, decision)]) != 1:
				raise ValueError("commitment receipt is missing from full global purchase allocations")
			state = LockedCommitmentState(
				self.inspection,
				self.purchase,
				item,
				capacity,
				frappe.session.user,
				commitment_session(item.reservation),
			)
			check_commitment_state_access(state)
			confirmation.check_commitment_confirmation_proof(
				state, confirmed, self.command.proof, at=decision.recorded_at
			)
			positions = [p for p in self.inspection.source_usage if p.reservation == item.reservation]
			if len(positions) != 1 or positions[0].last_sequence <= item.previous_sequence:
				raise ValueError("complete posted commitment history required")
			records = read_guarded_commitment_records(
				self.inspection.keys, item.reservation, positions[0].last_sequence
			)
			record = records[item.previous_sequence]
			if record.event.name != result["event"] or record != commitment_record(
				item,
				confirmed,
				decision,
				records[item.previous_sequence - 1],
				material_snapshot=first.material_snapshot_json,
				order_snapshot=first.order_snapshot_json,
				occurred_at=record.occurred_at,
			):
				raise ValueError("receipt event differs from independent commitment decision")
			if (
				second.owner != decision.actor
				or frappe.get_doc(EVENT, record.event.name, for_update=True).owner != decision.actor
			):
				raise ValueError("commitment owner differs from its recorded actor")
			check_commitment_state_access(state)
		except BaseException as error:
			poison(error)
