"""Private reservation confirmation primitives; not a posting or public API."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal, localcontext

import frappe
from frappe.utils import now_datetime

from medops.common.budget_ledger import LEDGER
from medops.common.budget_workflow import CONFIRMATION_SECONDS
from medops.common.confirmation_privacy import ConfirmationProof
from medops.common.material_source_snapshot import material_source_snapshot, parse_material_source_snapshot
from medops.common.reservation_authorization import (
	AUTHORIZATION_ROW_FIELDS,
	ReservationIntent,
	reservation_confirmation,
	reservation_decision,
	verify_reservation_confirmation_proof,
	verify_reservation_confirmation_row,
)
from medops.common.reservation_evidence_control import expected_reservation_evidence
from medops.hrp_budget.services.approval import require_web_session, session_digest
from medops.hrp_budget.services.control_guards import assert_budget_guards, invalidate_budget_guards
from medops.hrp_budget.services.material_budget_guards import assert_material_budget_guards
from medops.hrp_budget.services.material_request_source import locked_material_request_source
from medops.hrp_budget.services.material_usage_preview import inspect_purchase_usage
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.services.data_access import require_scope
from medops.services.errors import raise_ione_error, require_roles

DOCTYPE = "HRP Budget Reservation Authorization"
RESERVATION = "HRP Budget Reservation"


def _timestamp(value):
	return value.isoformat(sep=" ", timespec="microseconds")


def _session(item):
	return session_digest("budget-reservation:" + item.reservation_identity)


def require_reservation_actor_scope(item, actor, session):
	require_web_session()
	require_roles(MANAGER_ROLES)
	if frappe.session.user != actor or not hmac.compare_digest(_session(item), session):
		raise_ione_error("CONFLICT")
	for doctype, action in ((RESERVATION, "write"), (LEDGER, "read")):
		require_scope(
			doctype,
			company=item.key.company,
			hospital=item.key.hospital,
			organization_unit=item.key.organization_unit,
			action=action,
		)


def _inspect_intent(item, actor, session):
	if type(item) is not ReservationIntent:
		raise ValueError("validated reservation intent required")
	require_reservation_actor_scope(item, actor, session)
	facts = parse_material_source_snapshot(item.source_snapshot, expected_digest=item.source_snapshot_digest)
	inspection = inspect_purchase_usage(
		facts["request"],
		facts["line"],
		item.key.organization_unit,
		expected_source_digest=item.source_snapshot_digest,
		budget_date=item.budget_date,
		proposed_keys=[asdict(item.key)],
	)
	if not any(
		candidate.key == item.key
		and candidate.currency == item.currency
		and candidate.precision == item.precision
		for candidate in inspection.candidates
	):
		raise_ione_error("CONFLICT")
	observations = [
		observation
		for observation in inspection.budget_observations
		if (observation.key, observation.currency, observation.precision)
		== (item.key, item.currency, item.precision)
	]
	if len(observations) != 1 or Decimal(observations[0].difference) < Decimal(item.amount):
		raise_ione_error("CONFLICT")
	# This is a source-line limit, not a budget availability calculation.
	# Usage was independently bound to this locked source by the inspector.
	with localcontext() as context:
		context.prec = 48
		exposure = sum(
			(
				Decimal(position.open_reservation)
				+ Decimal(position.open_commitment)
				+ Decimal(position.actual)
				for position in inspection.source_usage
			),
			Decimal(0),
		)
		if exposure + Decimal(item.amount) > Decimal(inspection.source.amount):
			raise_ione_error("CONFLICT")
	_recheck(item, inspection.source, actor, session)
	return inspection.source


def _recheck(item, source, actor, session):
	require_reservation_actor_scope(item, actor, session)
	assert_material_budget_guards(source, [item.key])
	current = locked_material_request_source(source.request, source.line, source.organization_unit)
	snapshot, digest = material_source_snapshot(asdict(current))
	if current != source or snapshot != item.source_snapshot or digest != item.source_snapshot_digest:
		raise_ione_error("CONFLICT")
	assert_budget_guards([item.key])
	require_reservation_actor_scope(item, actor, session)


def revalidate_reservation_intent(item, *, actor, session):
	"""Recheck current access, source and guard, including on idempotent replay.

	The caller must start in an owned transaction and roll back after failure.
	This does not authorize a reservation or calculate available funds.
	"""
	try:
		require_web_session()
		require_reservation_actor_scope(item, actor, session)
		if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is None:
			_inspect_intent(item, actor, session)
		else:
			facts = parse_material_source_snapshot(
				item.source_snapshot, expected_digest=item.source_snapshot_digest
			)
			source = locked_material_request_source(
				facts["request"], facts["line"], item.key.organization_unit
			)
			_recheck(item, source, actor, session)
	except BaseException:
		invalidate_budget_guards()
		raise


def _append(item, confirmed, decision=None):
	with expected_reservation_evidence(item, confirmed, decision) as values:
		row = frappe.get_doc(dict(doctype=DOCTYPE, **values))
		row.insert(ignore_permissions=True)
	return row


def prepare_reservation_confirmation(item):
	"""Create a private preview in the caller's transaction; never posts funds.

	The caller must own a fresh transaction, enforce idempotency and audit, and
	protect the returned proof before exposing this through an HTTP endpoint.
	"""
	try:
		require_web_session()
		actor = frappe.session.user
		session = _session(item)
		source = _inspect_intent(item, actor, session)
		proof = ConfirmationProof(secrets.token_urlsafe(32))
		now = now_datetime()
		confirmed = reservation_confirmation(
			item,
			actor=actor,
			proof_hash=hashlib.sha256(proof.encode()).hexdigest(),
			session_digest=session,
			recorded_at=_timestamp(now),
			expires_at=_timestamp(now + timedelta(seconds=CONFIRMATION_SECONDS)),
		)
		row = _append(item, confirmed)
		_recheck(item, source, actor, session)
		return {
			"confirmation": row.name,
			"proof": proof,
			"expires_at": confirmed.expires_at,
			"intent_digest": item.intent_digest,
		}
	except BaseException:
		invalidate_budget_guards()
		raise


def confirm_reservation_confirmation(name, proof, acknowledge):
	"""Record an explicit session-bound decision; this does not reserve funds.

	The caller owns rollback/idempotency/audit and must keep proof out of logs.
	This function is deliberately not whitelisted or connected to a financial writer.
	"""
	try:
		require_web_session()
		require_roles(MANAGER_ROLES)
		if acknowledge is not True:
			raise_ione_error("INVALID_REQUEST")
		row = frappe.get_doc(DOCTYPE, name)
		data = {field: row.get(field) for field in AUTHORIZATION_ROW_FIELDS}
		item, confirmed = verify_reservation_confirmation_row(data)
		actor = frappe.session.user
		session = _session(item)
		try:
			verify_reservation_confirmation_proof(
				item,
				confirmed,
				proof=proof,
				actor=actor,
				session_digest=session,
				now=_timestamp(now_datetime()),
			)
		except ValueError as error:
			raise_ione_error("PERMISSION_DENIED", cause=error)
		source = _inspect_intent(item, actor, session)
		try:
			decision = reservation_decision(
				item,
				confirmed,
				proof=proof,
				actor=actor,
				session_digest=session,
				recorded_at=_timestamp(now_datetime()),
				acknowledge=True,
			)
		except ValueError as error:
			raise_ione_error("PERMISSION_DENIED", cause=error)
		if frappe.db.get_value(
			DOCTYPE, {"confirmation": confirmed.name}, "name", for_update=True
		) or frappe.db.get_value(DOCTYPE, {"one_time_key": decision.one_time_key}, "name", for_update=True):
			raise_ione_error("CONFLICT")
		row = _append(item, confirmed, decision)
		_recheck(item, source, actor, session)
		return {"decision": row.name, "intent_digest": item.intent_digest}
	except BaseException:
		invalidate_budget_guards()
		raise
