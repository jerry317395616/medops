"""Private audited renewal command; not connected to reservation posting."""

from __future__ import annotations

import hashlib

import frappe
from frappe.utils import now_datetime

from medops.common.domain_service import DomainServiceDefinition, normalize_sha256
from medops.common.material_source_snapshot import parse_material_source_snapshot
from medops.common.reservation_authorization import (
	AUTHORIZATION_ROW_FIELDS,
	verify_reservation_authorization_rows,
	verify_reservation_confirmation_proof,
	verify_reservation_confirmation_row,
)
from medops.common.reservation_renewal_request import RenewalCommand, build_renewal_command
from medops.hrp_budget.services.approval import require_web_session, session_digest
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.initial_reservation_write import (
	check_persisted_document,
	exact_new_document,
)
from medops.hrp_budget.services.material_budget_guards import assert_material_budget_guards
from medops.hrp_budget.services.material_request_source import locked_material_request_source
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.reservation_confirmation import (
	DOCTYPE as AUTHORIZATION,
)
from medops.hrp_budget.services.reservation_confirmation import (
	require_reservation_actor_scope,
	revalidate_reservation_intent,
)
from medops.hrp_budget.services.reservation_renewal_control import (
	DOCTYPE,
	enabled,
	expected_reservation_renewal,
	read_confirmation,
	require_unposted,
)
from medops.hrp_budget.services.reservation_renewal_lineage import locked_renewal_lineage
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error


def _original(command):
	rows = []
	for name in (command.original_confirmation, command.original_decision):
		doc = frappe.get_doc(AUTHORIZATION, name)
		rows.append({field: doc.get(field) for field in AUTHORIZATION_ROW_FIELDS})
	item, original = verify_reservation_authorization_rows(*rows)
	checked, confirmation = verify_reservation_confirmation_row(rows[0])
	if checked != item:
		raise ValueError("original reservation confirmation differs")
	return item, confirmation, original


def _proof(item, confirmation, proof, *, now):
	session = session_digest("budget-reservation:" + item.reservation_identity)
	try:
		verify_reservation_confirmation_proof(
			item, confirmation, proof=proof, actor=frappe.session.user, session_digest=session, now=now
		)
	except ValueError as error:
		raise_ione_error("PERMISSION_DENIED", cause=error)
	return session


def _source(item, actor, session):
	revalidate_reservation_intent(item, actor=actor, session=session)
	facts = parse_material_source_snapshot(item.source_snapshot, expected_digest=item.source_snapshot_digest)
	source = locked_material_request_source(facts["request"], facts["line"], item.key.organization_unit)
	assert_material_budget_guards(source, [item.key])
	return source


def append_reservation_renewal(command: RenewalCommand) -> dict[str, str]:
	"""Append a single renewal inside a caller-owned transaction; no commit."""
	try:
		enabled()
		require_web_session()
		if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is not None:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		item, _, _ = _original(command)
		checked, confirmed = read_confirmation(command.renewal_confirmation)
		if checked != item:
			raise_ione_error("CONFLICT")
		actor = frappe.session.user
		session = _proof(
			item, confirmed, command.proof, now=now_datetime().isoformat(sep=" ", timespec="microseconds")
		)
		source = _source(item, actor, session)
		require_unposted(item)
		with expected_reservation_renewal(
			item,
			source,
			command.original_confirmation,
			command.original_decision,
			command.renewal_confirmation,
			command.proof,
			acknowledge=command.acknowledge,
		) as state:
			doc = exact_new_document(DOCTYPE, state.values).insert(ignore_permissions=True)
		doc = frappe.get_doc(DOCTYPE, doc.name, for_update=True)
		check_persisted_document(doc, state.values, 0)
		lineage = locked_renewal_lineage(
			item, source, command.original_confirmation, command.original_decision
		)
		if not lineage.renewals or lineage.renewals[-1][1].name != doc.name:
			raise_ione_error("CONFLICT")
		checked, locked_confirmation = read_confirmation(command.renewal_confirmation, lock=True)
		if checked != item or locked_confirmation != confirmed:
			raise_ione_error("CONFLICT")
		_proof(item, confirmed, command.proof, now=now_datetime().isoformat(sep=" ", timespec="microseconds"))
		require_reservation_actor_scope(item, actor, session)
		require_unposted(item)
		assert_material_budget_guards(source, [item.key])
		return {"renewal": doc.name, "intent_digest": item.intent_digest, "expires_at": confirmed.expires_at}
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise


def verify_renewal_result(command: RenewalCommand, result: dict[str, object]) -> None:
	"""Recheck a completed receipt; proof expiry does not authorize a new write."""
	try:
		enabled()
		require_web_session()
		if set(result) != {"renewal", "intent_digest", "expires_at"}:
			raise ValueError("complete renewal receipt required")
		normalize_sha256(result["renewal"], label="renewal")
		normalize_sha256(result["intent_digest"], label="intent_digest")
		item, _, _ = _original(command)
		checked, confirmed = read_confirmation(command.renewal_confirmation)
		if checked != item:
			raise ValueError("confirmation differs")
		actor = frappe.session.user
		session = session_digest("budget-reservation:" + item.reservation_identity)
		source = _source(item, actor, session)
		require_unposted(item)
		lineage = locked_renewal_lineage(
			item, source, command.original_confirmation, command.original_decision
		)
		if not lineage.renewals:
			raise ValueError("renewal evidence is missing")
		last_confirmation, decision = lineage.renewals[-1]
		if (
			last_confirmation != confirmed
			or decision.name != result["renewal"]
			or result["intent_digest"] != item.intent_digest
			or result["expires_at"] != confirmed.expires_at
		):
			raise ValueError("receipt differs from latest renewal")
		_proof(item, confirmed, command.proof, now=decision.recorded_at)
		require_reservation_actor_scope(item, actor, session)
		assert_material_budget_guards(source, [item.key])
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise


class RenewReservationDecision(DomainService[RenewalCommand]):
	definition = DomainServiceDefinition(
		name="hrp_budget.reservation.renew", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		require_web_session()
		enabled()

	def validate(self, command):
		try:
			if (
				type(command) is not RenewalCommand
				or build_renewal_command(
					{
						"original_confirmation": command.original_confirmation,
						"original_decision": command.original_decision,
						"renewal_confirmation": command.renewal_confirmation,
						"proof": command.proof,
						"acknowledge": command.acknowledge,
					}
				)
				!= command
			):
				raise ValueError("canonical renewal command required")
		except ValueError as error:
			raise_ione_error("INVALID_REQUEST", cause=error)
		self.command = command

	def request_payload(self, command):
		return {
			"original_confirmation": command.original_confirmation,
			"original_decision": command.original_decision,
			"renewal_confirmation": command.renewal_confirmation,
			"proof_hash": hashlib.sha256(command.proof.encode()).hexdigest(),
			"acknowledge": True,
		}

	def perform(self, command):
		return append_reservation_renewal(command)

	def authorize_result(self, result):
		verify_renewal_result(self.command, result)
