"""Exact reservation confirmation contracts; no persistence or authority."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal

from medops.common.budget_control import BudgetControlKey
from medops.common.budget_reservation_anchor import reservation_source_identity
from medops.common.budget_reservation_proof import ReservationSource
from medops.common.budget_workflow import CONFIRMATION_SECONDS
from medops.common.domain_service import fingerprint_json, normalize_sha256
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.material_source_snapshot import parse_material_source_snapshot
from medops.common.organization import normalize_reference, normalize_required_date, normalize_required_text


@dataclass(frozen=True, slots=True)
class ReservationIntent:
	reservation_identity: str
	intent_digest: str
	source_snapshot: str = field(repr=False)
	source_snapshot_digest: str
	allocation: str
	key: BudgetControlKey
	currency: str
	precision: int
	amount: str
	budget_date: str
	reason: str = field(repr=False)


def reservation_intent(
	*,
	source_snapshot: object,
	source_snapshot_digest: object,
	allocation: object,
	key: object,
	currency: object,
	precision: object,
	amount: object,
	budget_date: object,
	reason: object,
) -> ReservationIntent:
	"""Bind the exact proposed action; this neither authenticates nor reserves."""
	digest = normalize_sha256(source_snapshot_digest, label="source_snapshot_digest")
	facts = parse_material_source_snapshot(source_snapshot, expected_digest=digest)
	if not isinstance(source_snapshot, str):
		raise ValueError("canonical source snapshot required")
	allocation_name = normalize_reference(allocation, label="allocation")
	if allocation_name != allocation or type(key) is not BudgetControlKey:
		raise ValueError("canonical allocation and budget key required")
	if (key.company, key.hospital, key.organization_unit, key.project) != (
		facts["company"],
		facts["hospital"],
		facts["organization_unit"],
		facts["project"],
	):
		raise ValueError("intent scope differs from source")
	unit = normalize_reference(currency, label="currency")
	if unit != currency or unit != facts["currency"] or type(precision) is not int or not 0 <= precision <= 9:
		raise ValueError("source currency and precision required")
	value = exact_decimal_amount(amount, precision, maximum=MAX_APPROVAL_AMOUNT)
	if value != amount or Decimal(value) <= 0 or Decimal(value) > Decimal(facts["amount"]):
		raise ValueError("canonical amount within source required")
	date = normalize_required_date(budget_date, label="budget_date")
	if date != budget_date or not key.period_start <= date <= key.period_end:
		raise ValueError("budget date must be inside the selected period")
	text = normalize_required_text(reason, label="reason", maximum=1000)
	if text != reason:
		raise ValueError("canonical intent reason required")
	source = ReservationSource(
		"Material Request", facts["request"], digest, facts["line"], allocation_name, "Reserve"
	)
	identity = reservation_source_identity(source)
	content = {
		"schema_version": 1,
		"kind": "material_reservation_authorization",
		"action": "Reserve",
		"reservation_identity": identity,
		"source_snapshot_digest": digest,
		"allocation": allocation_name,
		"budget_key": asdict(key),
		"currency": unit,
		"precision": precision,
		"amount": value,
		"budget_date": date,
		"reason": text,
	}
	return ReservationIntent(
		identity,
		fingerprint_json(content),
		source_snapshot,
		digest,
		allocation_name,
		key,
		unit,
		precision,
		value,
		date,
		text,
	)


def _require_intent(value: object) -> ReservationIntent:
	if type(value) is not ReservationIntent:
		raise ValueError("canonical reservation intent required")
	actual = reservation_intent(
		source_snapshot=value.source_snapshot,
		source_snapshot_digest=value.source_snapshot_digest,
		allocation=value.allocation,
		key=value.key,
		currency=value.currency,
		precision=value.precision,
		amount=value.amount,
		budget_date=value.budget_date,
		reason=value.reason,
	)
	if actual != value:
		raise ValueError("reservation intent differs from bound facts")
	return actual


def _time(value: object) -> datetime:
	if not isinstance(value, str):
		raise ValueError("canonical confirmation time required")
	try:
		stamp = datetime.fromisoformat(value)
	except ValueError as error:
		raise ValueError("invalid confirmation time") from error
	if stamp.tzinfo is not None or stamp.isoformat(sep=" ", timespec="microseconds") != value:
		raise ValueError("canonical confirmation time required")
	return stamp


@dataclass(frozen=True, slots=True)
class ReservationConfirmation:
	name: str
	reservation_identity: str
	intent_digest: str
	source_snapshot_digest: str
	actor: str
	proof_hash: str
	session_digest: str
	recorded_at: str
	expires_at: str
	event_digest: str


def reservation_confirmation(
	intent: object,
	*,
	actor: object,
	proof_hash: object,
	session_digest: object,
	recorded_at: object,
	expires_at: object,
) -> ReservationConfirmation:
	"""Construct a proposed confirmation; trusted storage and session are external."""
	item = _require_intent(intent)
	user = normalize_reference(actor, label="actor")
	if user != actor:
		raise ValueError("canonical actor required")
	proof = normalize_sha256(proof_hash, label="proof_hash")
	session = normalize_sha256(session_digest, label="session_digest")
	if not isinstance(recorded_at, str) or not isinstance(expires_at, str):
		raise ValueError("canonical confirmation time required")
	start, end = _time(recorded_at), _time(expires_at)
	seconds = (end - start).total_seconds()
	if not 0 < seconds <= CONFIRMATION_SECONDS:
		raise ValueError("confirmation lifetime exceeds policy")
	content = {
		"schema_version": 1,
		"kind": "reservation_confirmation",
		"reservation_identity": item.reservation_identity,
		"intent_digest": item.intent_digest,
		"source_snapshot_digest": item.source_snapshot_digest,
		"actor": user,
		"proof_hash": proof,
		"session_digest": session,
		"recorded_at": recorded_at,
		"expires_at": expires_at,
	}
	key = fingerprint_json({"namespace": "budget-reservation-confirmation-v1", **content})
	return ReservationConfirmation(
		key,
		item.reservation_identity,
		item.intent_digest,
		item.source_snapshot_digest,
		user,
		proof,
		session,
		recorded_at,
		expires_at,
		fingerprint_json({"name": key, **content}),
	)


def _require_confirmation(intent: ReservationIntent, value: object) -> ReservationConfirmation:
	if type(value) is not ReservationConfirmation:
		raise ValueError("canonical confirmation required")
	expected = reservation_confirmation(
		intent,
		actor=value.actor,
		proof_hash=value.proof_hash,
		session_digest=value.session_digest,
		recorded_at=value.recorded_at,
		expires_at=value.expires_at,
	)
	if expected != value:
		raise ValueError("confirmation differs from intent")
	return expected


def verify_reservation_confirmation_proof(
	intent: object,
	confirmation: object,
	*,
	proof: object,
	actor: object,
	session_digest: object,
	now: object,
) -> None:
	"""Check proof binding, never infer a real human or durable authorization."""
	item = _require_intent(intent)
	confirmation = _require_confirmation(item, confirmation)
	provided_session = normalize_sha256(session_digest, label="session_digest")
	if (
		not isinstance(proof, str)
		or not 32 <= len(proof) <= 128
		or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in proof)
		or actor != confirmation.actor
		or not hmac.compare_digest(provided_session, confirmation.session_digest)
		or not hmac.compare_digest(hashlib.sha256(proof.encode("ascii")).hexdigest(), confirmation.proof_hash)
		or not _time(confirmation.recorded_at) <= _time(now) < _time(confirmation.expires_at)
	):
		raise ValueError("confirmation proof, actor, session or time differs")


@dataclass(frozen=True, slots=True)
class ReservationDecision:
	name: str
	reservation_identity: str
	intent_digest: str
	source_snapshot_digest: str
	confirmation: str
	actor: str
	proof_hash: str
	session_digest: str
	recorded_at: str
	expires_at: str
	acknowledged: bool
	one_time_key: str
	event_digest: str


def reservation_decision(
	intent: object,
	confirmation: object,
	*,
	proof: object,
	actor: object,
	session_digest: object,
	recorded_at: object,
	acknowledge: object,
) -> ReservationDecision:
	"""Build a decision after explicit acknowledgement and checked proof.

	The caller must authenticate a real web session, permissions and current
	source facts, then persist once in an immutable controlled transaction. This
	function itself does none of those things and grants no posting authority.
	"""
	if acknowledge is not True:
		raise ValueError("explicit acknowledgement required")
	if not isinstance(recorded_at, str):
		raise ValueError("canonical decision time required")
	item = _require_intent(intent)
	confirmation = _require_confirmation(item, confirmation)
	verify_reservation_confirmation_proof(
		item, confirmation, proof=proof, actor=actor, session_digest=session_digest, now=recorded_at
	)
	return _decision_record(item, confirmation, recorded_at)


def _decision_record(
	item: ReservationIntent, confirmation: ReservationConfirmation, recorded_at: str
) -> ReservationDecision:
	if not _time(confirmation.recorded_at) <= _time(recorded_at) < _time(confirmation.expires_at):
		raise ValueError("decision is outside the confirmation window")
	one_time = fingerprint_json(
		{"namespace": "budget-reservation-decision-v1", "reservation_identity": item.reservation_identity}
	)
	content = {
		"schema_version": 1,
		"kind": "reservation_decision",
		"reservation_identity": item.reservation_identity,
		"intent_digest": item.intent_digest,
		"source_snapshot_digest": item.source_snapshot_digest,
		"confirmation": confirmation.name,
		"actor": confirmation.actor,
		"proof_hash": confirmation.proof_hash,
		"session_digest": confirmation.session_digest,
		"recorded_at": recorded_at,
		"expires_at": confirmation.expires_at,
		"acknowledged": True,
		"one_time_key": one_time,
	}
	name = fingerprint_json({"namespace": "budget-reservation-decision-event-v1", **content})
	return ReservationDecision(
		name,
		item.reservation_identity,
		item.intent_digest,
		item.source_snapshot_digest,
		confirmation.name,
		confirmation.actor,
		confirmation.proof_hash,
		confirmation.session_digest,
		recorded_at,
		confirmation.expires_at,
		True,
		one_time,
		fingerprint_json({"name": name, **content}),
	)


def verify_reservation_decision_evidence(
	intent: object, confirmation: object, decision: object
) -> ReservationDecision:
	"""Check a historical decision without retaining the plaintext proof.

	A valid hash chain is not an authenticated human action. The caller must
	independently verify immutable controlled storage, web-session authorization,
	unique consumption, and current rights before using the evidence.
	"""
	item = _require_intent(intent)
	confirmed = _require_confirmation(item, confirmation)
	if type(decision) is not ReservationDecision or decision.acknowledged is not True:
		raise ValueError("explicit decision evidence required")
	expected = _decision_record(item, confirmed, decision.recorded_at)
	if decision != expected:
		raise ValueError("decision differs from confirmation and intent")
	return expected
