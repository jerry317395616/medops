"""Audited, idempotent private reservation confirmation services.

There is intentionally no whitelisted API or financial posting integration yet.
"""

from __future__ import annotations

import hashlib

import frappe
from frappe.utils import now_datetime

from medops.common.budget_workflow import HumanConfirmation, build_human_confirmation
from medops.common.domain_service import DomainServiceDefinition
from medops.common.reservation_authorization import (
	AUTHORIZATION_ROW_FIELDS,
	ReservationIntent,
	reservation_intent_snapshot,
	verify_reservation_authorization_rows,
	verify_reservation_confirmation_proof,
	verify_reservation_confirmation_row,
)
from medops.hrp_budget.services.approval import require_web_session, session_digest
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.reservation_confirmation import (
	DOCTYPE,
	confirm_reservation_confirmation,
	prepare_reservation_confirmation,
	revalidate_reservation_intent,
)
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error


def _read_evidence(name):
	row = frappe.get_doc(DOCTYPE, name)
	return {field: row.get(field) for field in AUTHORIZATION_ROW_FIELDS}


def _checked_confirmation(name, proof, *, recorded_at=None):
	item, confirmed = verify_reservation_confirmation_row(_read_evidence(name))

	def verify_current():
		stamp = recorded_at or now_datetime().isoformat(sep=" ", timespec="microseconds")
		current_session = session_digest("budget-reservation:" + item.reservation_identity)
		verify_reservation_confirmation_proof(
			item, confirmed, proof=proof, actor=frappe.session.user, session_digest=current_session, now=stamp
		)

	verify_current()
	revalidate_reservation_intent(item, actor=confirmed.actor, session=confirmed.session_digest)
	verify_current()
	return item, confirmed


class PrepareReservationConfirmation(DomainService[ReservationIntent]):
	definition = DomainServiceDefinition(
		name="hrp_budget.reservation.preview", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		require_web_session()

	def validate(self, command):
		reservation_intent_snapshot(command)
		self.intent = command

	def request_payload(self, command):
		return {"intent_digest": command.intent_digest}

	def perform(self, command):
		return prepare_reservation_confirmation(command)

	def authorize_result(self, result):
		if set(result) != {"confirmation", "proof", "expires_at", "intent_digest"}:
			raise_ione_error("CONFLICT")
		item, confirmed = _checked_confirmation(result["confirmation"], result["proof"])
		if (
			item != self.intent
			or result["intent_digest"] != item.intent_digest
			or result["expires_at"] != confirmed.expires_at
		):
			raise_ione_error("CONFLICT")


class ConfirmReservationDecision(DomainService[HumanConfirmation]):
	definition = DomainServiceDefinition(
		name="hrp_budget.reservation.confirm", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		require_web_session()

	def validate(self, command):
		if (
			type(command) is not HumanConfirmation
			or build_human_confirmation(
				{
					"confirmation": command.confirmation,
					"proof": command.proof,
					"acknowledge": command.acknowledge,
				}
			)
			!= command
		):
			raise_ione_error("INVALID_REQUEST")
		self.confirmation = command

	def request_payload(self, command):
		return {
			"confirmation": command.confirmation,
			"proof_hash": hashlib.sha256(command.proof.encode()).hexdigest(),
			"acknowledge": True,
		}

	def perform(self, command):
		return confirm_reservation_confirmation(command.confirmation, command.proof, command.acknowledge)

	def authorize_result(self, result):
		if set(result) != {"decision", "intent_digest"}:
			raise_ione_error("CONFLICT")
		first = _read_evidence(self.confirmation.confirmation)
		second = _read_evidence(result["decision"])
		item, decision = verify_reservation_authorization_rows(first, second)
		if (
			result["intent_digest"] != item.intent_digest
			or decision.confirmation != self.confirmation.confirmation
		):
			raise_ione_error("CONFLICT")
		checked, _ = _checked_confirmation(
			self.confirmation.confirmation, self.confirmation.proof, recorded_at=decision.recorded_at
		)
		if checked != item:
			raise_ione_error("CONFLICT")
