"""Exact reservation confirmation contracts; no persistence or authority."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal

from medops.common.budget_control import BudgetControlKey, budget_control_key
from medops.common.budget_reservation_anchor import reservation_source_identity
from medops.common.budget_reservation_proof import ReservationSource
from medops.common.budget_workflow import CONFIRMATION_SECONDS
from medops.common.domain_service import canonical_json, fingerprint_json, normalize_sha256
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


def reservation_intent_snapshot(intent: object) -> str:
	"""Serialize the complete intent without embedding the separately held source."""
	item = _require_intent(intent)
	return canonical_json(
		{
			"schema_version": 1,
			"kind": "material_reservation_intent",
			"reservation_identity": item.reservation_identity,
			"intent_digest": item.intent_digest,
			"source_snapshot_digest": item.source_snapshot_digest,
			"allocation": item.allocation,
			"budget_key": asdict(item.key),
			"currency": item.currency,
			"precision": item.precision,
			"amount": item.amount,
			"budget_date": item.budget_date,
			"reason": item.reason,
		}
	)


def _unique_json_object(pairs):
	result = {}
	for name, value in pairs:
		if name in result:
			raise ValueError("duplicate intent key")
		result[name] = value
	return result


def parse_reservation_intent_snapshot(value: object, *, source_snapshot: object) -> ReservationIntent:
	"""Reconstruct exact persisted intent data without granting authority.

	The source and intent JSON must be read from controlled immutable storage;
	the caller must separately verify the human decision and current permission.
	"""
	if not isinstance(value, str) or len(value.encode("utf-8")) > 32768:
		raise ValueError("bounded intent snapshot required")
	try:
		data = json.loads(value, object_pairs_hook=_unique_json_object)
	except (TypeError, ValueError, RecursionError) as error:
		raise ValueError("invalid intent snapshot") from error
	fields = {
		"schema_version",
		"kind",
		"reservation_identity",
		"intent_digest",
		"source_snapshot_digest",
		"allocation",
		"budget_key",
		"currency",
		"precision",
		"amount",
		"budget_date",
		"reason",
	}
	if (
		not isinstance(data, dict)
		or set(data) != fields
		or type(data["schema_version"]) is not int
		or data["schema_version"] != 1
		or data["kind"] != "material_reservation_intent"
		or canonical_json(data) != value
	):
		raise ValueError("invalid canonical intent snapshot")
	key = budget_control_key(data["budget_key"])
	if asdict(key) != data["budget_key"]:
		raise ValueError("noncanonical budget key")
	item = reservation_intent(
		source_snapshot=source_snapshot,
		source_snapshot_digest=data["source_snapshot_digest"],
		allocation=data["allocation"],
		key=key,
		currency=data["currency"],
		precision=data["precision"],
		amount=data["amount"],
		budget_date=data["budget_date"],
		reason=data["reason"],
	)
	if (
		item.reservation_identity != data["reservation_identity"]
		or item.intent_digest != data["intent_digest"]
	):
		raise ValueError("persisted intent digest differs")
	return item


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
	source = parse_material_source_snapshot(item.source_snapshot, expected_digest=item.source_snapshot_digest)
	if any(datetime.fromisoformat(source[field]) > start for field in ("request_modified", "line_modified")):
		raise ValueError("confirmation predates the source snapshot")
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


AUTHORIZATION_ROW_FIELDS = frozenset(
	{
		"name",
		"evidence_key",
		"event_kind",
		"reservation_identity",
		"intent_digest",
		"source_snapshot_digest",
		"budget_key_digest",
		"company",
		"hospital",
		"organization_unit",
		"source_name",
		"source_line",
		"actor",
		"proof_hash",
		"session_digest",
		"recorded_at",
		"expires_at",
		"confirmation",
		"acknowledged",
		"one_time_key",
		"event_digest",
		"intent_snapshot_json",
		"source_snapshot_json",
		"proof_version",
	}
)


def reservation_authorization_row(
	intent: object, confirmation: object, decision: object | None = None
) -> dict[str, object]:
	"""Project checked evidence; this never writes or authenticates it."""
	item = _require_intent(intent)
	confirmed = _require_confirmation(item, confirmation)
	decided = None if decision is None else verify_reservation_decision_evidence(item, confirmed, decision)
	selected = confirmed if decided is None else decided
	facts = parse_material_source_snapshot(item.source_snapshot, expected_digest=item.source_snapshot_digest)
	return {
		"name": selected.name,
		"evidence_key": selected.name,
		"event_kind": "Confirmation" if decision is None else "Decision",
		"reservation_identity": item.reservation_identity,
		"intent_digest": item.intent_digest,
		"source_snapshot_digest": item.source_snapshot_digest,
		"budget_key_digest": item.key.identity,
		"company": item.key.company,
		"hospital": item.key.hospital,
		"organization_unit": item.key.organization_unit,
		"source_name": facts["request"],
		"source_line": facts["line"],
		"actor": selected.actor,
		"proof_hash": selected.proof_hash,
		"session_digest": selected.session_digest,
		"recorded_at": selected.recorded_at,
		"expires_at": selected.expires_at,
		"confirmation": None if decided is None else confirmed.name,
		"acknowledged": 0 if decided is None else 1,
		"one_time_key": None if decided is None else decided.one_time_key,
		"event_digest": selected.event_digest,
		"intent_snapshot_json": reservation_intent_snapshot(item),
		"source_snapshot_json": item.source_snapshot,
		"proof_version": 1,
	}


def _stored_row(value: object) -> dict[str, object]:
	if not isinstance(value, dict) or set(value) != AUTHORIZATION_ROW_FIELDS:
		raise ValueError("complete authorization evidence row required")
	row = dict(value)
	if type(row["proof_version"]) is not int or row["proof_version"] != 1:
		raise ValueError("unsupported authorization evidence version")
	if type(row["acknowledged"]) is not int or row["acknowledged"] not in (0, 1):
		raise ValueError("canonical acknowledgement required")
	for column in ("recorded_at", "expires_at"):
		value = row[column]
		if isinstance(value, datetime) and value.tzinfo is None:
			row[column] = value.isoformat(sep=" ", timespec="microseconds")
		elif isinstance(value, str):
			try:
				parsed = datetime.fromisoformat(value)
			except ValueError as exc:
				raise ValueError("canonical stored evidence time required") from exc
			if parsed.tzinfo is not None or value not in (
				parsed.isoformat(sep=" ", timespec="seconds"),
				parsed.isoformat(sep=" ", timespec="microseconds"),
			):
				raise ValueError("canonical stored evidence time required")
			row[column] = parsed.isoformat(sep=" ", timespec="microseconds")
		else:
			raise ValueError("canonical stored evidence time required")
	for column in ("confirmation", "one_time_key"):
		if row[column] in (None, ""):
			row[column] = None
	return row


def verify_reservation_confirmation_row(
	confirmation_row: object,
) -> tuple[ReservationIntent, ReservationConfirmation]:
	"""Reconstruct a confirmation without treating its contents as authority."""
	first = _stored_row(confirmation_row)
	item = parse_reservation_intent_snapshot(
		first["intent_snapshot_json"], source_snapshot=first["source_snapshot_json"]
	)
	confirmed = reservation_confirmation(
		item,
		actor=first["actor"],
		proof_hash=first["proof_hash"],
		session_digest=first["session_digest"],
		recorded_at=first["recorded_at"],
		expires_at=first["expires_at"],
	)
	if reservation_authorization_row(item, confirmed) != first:
		raise ValueError("stored confirmation differs from intent")
	return item, confirmed


def verify_reservation_authorization_rows(
	confirmation_row: object, decision_row: object
) -> tuple[ReservationIntent, ReservationDecision]:
	"""Verify exact stored projections, but not their human or storage authority.

	The caller must establish immutable controlled insertion, unique consumption,
	a real authenticated web session, full source access and transactional scope.
	A self-consistent row pair alone never authorizes a financial action.
	"""
	item, confirmed = verify_reservation_confirmation_row(confirmation_row)
	second = _stored_row(decision_row)
	if not isinstance(second["recorded_at"], str):
		raise ValueError("canonical decision time required")
	decision = _decision_record(item, confirmed, second["recorded_at"])
	if reservation_authorization_row(item, confirmed, decision) != second:
		raise ValueError("stored decision differs from confirmation")
	return item, decision
