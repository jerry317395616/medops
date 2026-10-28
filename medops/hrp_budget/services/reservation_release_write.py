"""Private, default-disabled independent confirmation and atomic reservation release."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from medops.common.budget_reservation_proof import verify_reservation_records
from medops.common.budget_workflow import CONFIRMATION_SECONDS
from medops.common.confirmation_privacy import ConfirmationProof
from medops.common.domain_service import DomainServiceDefinition, normalize_sha256
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.reservation_release import (
	RELEASE_ACTION_PREFIX,
	RELEASE_ROW_FIELDS,
	reservation_release_confirmation,
	reservation_release_intent,
	reservation_release_row,
	verify_reservation_release_row,
)
from medops.common.reservation_release_request import (
	ReleaseReservationPost,
	ReleaseReservationPrepare,
	build_release_reservation_post,
	build_release_reservation_prepare,
)
from medops.hrp_budget.services.approval import require_web_session
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.initial_reservation_write import check_persisted_document, exact_new_document
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.reservation_release_control import (
	DOCTYPE,
	EVENT,
	check_proof,
	enabled,
	expected_reservation_release,
	read_release_confirmation,
	release_session,
)
from medops.hrp_budget.services.reservation_release_state import (
	inspect_reservation_release,
	recheck_reservation_release,
)
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error

PREPARE_RESULT_FIELDS = {
	"confirmation",
	"proof",
	"expires_at",
	"intent_digest",
	"reservation",
	"amount",
	"currency",
	"precision",
	"open_before",
}


def _intent(history, command):
	amount = exact_decimal_amount(command.amount, history.original.precision, maximum=MAX_APPROVAL_AMOUNT)
	return reservation_release_intent(
		history.original, history.position, history.records[-1], amount=amount, reason=command.reason
	)


def _result_history(item, owned_history):
	if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is None:
		return inspect_reservation_release(item.reservation)
	if owned_history is None:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	return recheck_reservation_release(owned_history)


def _poison(error):
	invalidate_budget_guards()
	if isinstance(error, (ValueError, TypeError, KeyError)):
		raise_ione_error("CONFLICT", cause=error)
	raise error


class PrepareReservationRelease(DomainService[ReleaseReservationPrepare]):
	definition = DomainServiceDefinition(
		name="hrp_budget.reservation_release.preview", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		enabled()
		require_web_session()

	def validate(self, command):
		try:
			if (
				type(command) is not ReleaseReservationPrepare
				or build_release_reservation_prepare(
					{"reservation": command.reservation, "amount": command.amount, "reason": command.reason}
				)
				!= command
			):
				raise ValueError("canonical release preview command required")
		except (ValueError, TypeError, KeyError) as error:
			raise_ione_error("INVALID_REQUEST", cause=error)
		self.command = command
		self.history = None

	def request_payload(self, command):
		return {
			"reservation": command.reservation,
			"amount": command.amount,
			"reason_hash": hashlib.sha256(command.reason.encode()).hexdigest(),
		}

	def perform(self, command):
		try:
			history = inspect_reservation_release(command.reservation)
			item = _intent(history, command)
			proof = ConfirmationProof(secrets.token_urlsafe(32))
			start = now_datetime()
			confirmed = reservation_release_confirmation(
				item,
				actor=frappe.session.user,
				proof_hash=hashlib.sha256(proof.encode()).hexdigest(),
				session_digest=release_session(item.reservation),
				recorded_at=start.isoformat(sep=" ", timespec="microseconds"),
				expires_at=(start + timedelta(seconds=CONFIRMATION_SECONDS)).isoformat(
					sep=" ", timespec="microseconds"
				),
			)
			with expected_reservation_release(history, item, confirmed, proof) as state:
				doc = exact_new_document(DOCTYPE, state.values).insert(ignore_permissions=True)
			check_persisted_document(
				frappe.get_doc(DOCTYPE, doc.name, for_update=True),
				reservation_release_row(item, confirmed),
				0,
			)
			self.history = recheck_reservation_release(history)
			return {
				"confirmation": confirmed.name,
				"proof": proof,
				"expires_at": confirmed.expires_at,
				"intent_digest": item.intent_digest,
				"reservation": item.reservation,
				"amount": item.amount,
				"currency": item.currency,
				"precision": item.precision,
				"open_before": item.open_before,
			}
		except BaseException as error:
			_poison(error)

	def authorize_result(self, result):
		try:
			enabled()
			if set(result) != PREPARE_RESULT_FIELDS:
				raise ValueError("complete release preview result required")
			item, confirmed = read_release_confirmation(result["confirmation"])
			check_proof(item, confirmed, result["proof"])
			history = _result_history(item, self.history)
			if read_release_confirmation(result["confirmation"], lock=True) != (item, confirmed):
				raise ValueError("release confirmation changed during inspection")
			if (
				_intent(history, self.command) != item
				or any(
					result[key] != getattr(item, key)
					for key in (
						"reservation",
						"amount",
						"currency",
						"precision",
						"open_before",
						"intent_digest",
					)
				)
				or result["expires_at"] != confirmed.expires_at
			):
				raise ValueError("release preview no longer matches current state")
			check_proof(item, confirmed, result["proof"])
		except BaseException as error:
			_poison(error)


class PostReservationRelease(DomainService[ReleaseReservationPost]):
	definition = DomainServiceDefinition(
		name="hrp_budget.reservation_release.post", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		enabled()
		require_web_session()

	def validate(self, command):
		try:
			if (
				type(command) is not ReleaseReservationPost
				or build_release_reservation_post(
					{
						"confirmation": command.confirmation,
						"proof": command.proof,
						"acknowledge": command.acknowledge,
					}
				)
				!= command
			):
				raise ValueError("canonical release post command required")
		except (ValueError, TypeError, KeyError) as error:
			raise_ione_error("INVALID_REQUEST", cause=error)
		self.command = command
		self.history = None

	def request_payload(self, command):
		return {
			"confirmation": command.confirmation,
			"proof_hash": hashlib.sha256(command.proof.encode()).hexdigest(),
			"acknowledge": True,
		}

	def perform(self, command):
		try:
			item, confirmed = read_release_confirmation(command.confirmation)
			check_proof(item, confirmed, command.proof)
			history = inspect_reservation_release(item.reservation)
			with expected_reservation_release(
				history, item, confirmed, command.proof, post=True, acknowledge=command.acknowledge
			) as state:
				exact_new_document(DOCTYPE, state.values).insert(ignore_permissions=True)
				exact_new_document(EVENT, state.event_values).insert(ignore_permissions=True)
			if state.decision is None or state.record is None:
				raise_ione_error("CONFLICT")
			check_persisted_document(
				frappe.get_doc(DOCTYPE, state.decision.name, for_update=True), state.values, 0
			)
			check_persisted_document(
				frappe.get_doc(EVENT, state.record.event.name, for_update=True), state.event_values, 0
			)
			position = verify_reservation_records([*history.records, state.record])
			self.history = recheck_reservation_release(history, position=position)
			return {
				"reservation": item.reservation,
				"release_decision": state.decision.name,
				"event": state.record.event.name,
				"intent_digest": item.intent_digest,
			}
		except BaseException as error:
			_poison(error)

	def authorize_result(self, result):
		try:
			enabled()
			if set(result) != {"reservation", "release_decision", "event", "intent_digest"}:
				raise ValueError("complete posted release result required")
			for key in result:
				normalize_sha256(result[key], label=key)
			item, confirmed = read_release_confirmation(self.command.confirmation)
			doc = frappe.get_doc(DOCTYPE, result["release_decision"])
			checked, decision = verify_reservation_release_row(
				{key: doc.get(key) for key in RELEASE_ROW_FIELDS}, confirmed
			)
			check_persisted_document(doc, reservation_release_row(item, decision, confirmed), 0)
			check_proof(item, confirmed, self.command.proof, at=decision.recorded_at)
			history = _result_history(item, self.history)
			if read_release_confirmation(self.command.confirmation, lock=True) != (item, confirmed):
				raise ValueError("release confirmation changed during receipt inspection")
			locked = frappe.get_doc(DOCTYPE, result["release_decision"], for_update=True)
			check_persisted_document(locked, reservation_release_row(item, decision, confirmed), 0)
			if (
				item != checked
				or result["reservation"] != item.reservation
				or result["intent_digest"] != item.intent_digest
				or len(history.records) <= item.previous_sequence
			):
				raise ValueError("posted release identity or history differs")
			record = history.records[item.previous_sequence]
			if (
				record.event.name != result["event"]
				or record.source.action != RELEASE_ACTION_PREFIX + decision.name
			):
				raise ValueError("posted release event differs from independent decision")
			recheck_reservation_release(history)
		except BaseException as error:
			_poison(error)
