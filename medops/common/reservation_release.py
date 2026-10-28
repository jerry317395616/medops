"""Exact release intent, independent evidence and immutable history contracts.

These functions do not establish storage authority, complete database history,
an authenticated person, current source rights, locks or a transaction boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import Any

from medops.common.budget_control import KEY_FIELDS, BudgetControlKey, budget_control_key
from medops.common.budget_reservation_events import (
	MAX_RESERVATION_EVENTS,
	ReservationEvent,
	ReservationPosition,
)
from medops.common.budget_reservation_proof import (
	ReservationRecord,
	reservation_event_key,
	reservation_record,
	verify_reservation_records,
)
from medops.common.budget_workflow import CONFIRMATION_SECONDS
from medops.common.domain_service import canonical_json, fingerprint_json, normalize_sha256
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.organization import normalize_reference, normalize_required_text
from medops.common.reservation_authorization import reservation_intent_snapshot

RELEASE_ACTION_PREFIX = "Release Reservation:"


def _time(value: object) -> datetime:
	if not isinstance(value, str):
		raise ValueError("canonical release time required")
	try:
		stamp = datetime.fromisoformat(value)
	except ValueError as error:
		raise ValueError("canonical release time required") from error
	if stamp.tzinfo is not None or stamp.isoformat(sep=" ", timespec="microseconds") != value:
		raise ValueError("canonical release time required")
	return stamp


@dataclass(frozen=True, slots=True)
class ReservationReleaseIntent:
	reservation: str
	original_intent_digest: str
	source_snapshot_digest: str
	key: BudgetControlKey
	currency: str
	precision: int
	previous_sequence: int
	previous_event_digest: str
	previous_occurred_at: str
	open_before: str
	amount: str
	reason: str = field(repr=False)
	intent_digest: str


def _intent(values):
	data = dict(values)
	for name in ("reservation", "currency"):
		if normalize_reference(data[name], label=name) != data[name]:
			raise ValueError("canonical release identity required")
	for name in ("original_intent_digest", "source_snapshot_digest", "previous_event_digest"):
		if normalize_sha256(data[name], label=name) != data[name]:
			raise ValueError("canonical release digest required")
	if (
		type(data["key"]) is not BudgetControlKey
		or type(data["precision"]) is not int
		or not 0 <= data["precision"] <= 9
		or type(data["previous_sequence"]) is not int
		or not 1 <= data["previous_sequence"] < MAX_RESERVATION_EVENTS
	):
		raise ValueError("canonical release scope, precision and sequence required")
	_time(data["previous_occurred_at"])
	for name in ("amount", "open_before"):
		value = exact_decimal_amount(data[name], data["precision"], maximum=MAX_APPROVAL_AMOUNT)
		if value != data[name] or Decimal(value) <= 0:
			raise ValueError("positive canonical release amount required")
	if Decimal(data["amount"]) > Decimal(data["open_before"]):
		raise ValueError("release exceeds open reservation")
	if normalize_required_text(data["reason"], label="reason", maximum=1000) != data["reason"]:
		raise ValueError("canonical release reason required")
	if budget_control_key(asdict(data["key"])) != data["key"]:
		raise ValueError("canonical release budget key required")
	digest = fingerprint_json(
		{"namespace": "budget-reservation-release-intent-v1", **data, "key": asdict(data["key"])}
	)
	return ReservationReleaseIntent(**data, intent_digest=digest)


def reservation_release_intent(original, position, last, *, amount, reason) -> ReservationReleaseIntent:
	"""Bind a release to a previously verified complete history and its exact tail.

	The supplied position is not authority. The caller must independently verify
	the initial decision and every later event before using this constructor.
	"""
	reservation_intent_snapshot(original)
	if type(position) is not ReservationPosition or type(last) is not ReservationRecord:
		raise ValueError("checked reservation position and tail required")
	if (
		(position.reservation, position.last_sequence, position.key, position.currency, position.precision)
		!= (last.event.reservation, last.event.sequence, original.key, original.currency, original.precision)
		or last.event.key != original.key
		or last.event.currency != original.currency
		or last.event.precision != original.precision
		or position.reserved != original.amount
		or any(Decimal(getattr(position, name)) != 0 for name in ("committed", "actual", "open_commitment"))
	):
		raise ValueError("only the independently verified uncommitted reservation may be released")
	with localcontext() as context:
		context.prec = 48
		if Decimal(position.open_reservation) + Decimal(position.released) != Decimal(original.amount):
			raise ValueError("release position differs from original amount")
	return _intent(
		dict(
			reservation=position.reservation,
			original_intent_digest=original.intent_digest,
			source_snapshot_digest=original.source_snapshot_digest,
			key=original.key,
			currency=original.currency,
			precision=original.precision,
			previous_sequence=position.last_sequence,
			previous_event_digest=last.event_digest,
			previous_occurred_at=last.occurred_at,
			open_before=position.open_reservation,
			amount=amount,
			reason=reason,
		)
	)


def _require_intent(value):
	if type(value) is not ReservationReleaseIntent:
		raise ValueError("canonical release intent required")
	data = {name: getattr(value, name) for name in value.__dataclass_fields__ if name != "intent_digest"}
	checked = _intent(data)
	if checked != value:
		raise ValueError("release intent differs from its digest")
	return checked


def reservation_release_snapshot(value) -> str:
	item = _require_intent(value)
	return canonical_json({"schema_version": 1, "kind": "reservation_release_intent", **asdict(item)})


def _unique_object(pairs):
	result = {}
	for key, value in pairs:
		if key in result:
			raise ValueError("duplicate release intent key")
		result[key] = value
	return result


def parse_reservation_release_snapshot(value) -> ReservationReleaseIntent:
	if not isinstance(value, str) or len(value.encode("utf-8")) > 32768:
		raise ValueError("bounded release intent snapshot required")
	try:
		data = json.loads(value, object_pairs_hook=_unique_object)
	except (ValueError, TypeError, RecursionError) as error:
		raise ValueError("invalid release intent snapshot") from error
	if (
		not isinstance(data, dict)
		or set(data) != set(ReservationReleaseIntent.__dataclass_fields__) | {"schema_version", "kind"}
		or type(data["schema_version"]) is not int
		or data["schema_version"] != 1
		or data["kind"] != "reservation_release_intent"
		or canonical_json(data) != value
	):
		raise ValueError("canonical release intent snapshot required")
	key = budget_control_key(data["key"])
	if asdict(key) != data["key"]:
		raise ValueError("canonical release budget key required")
	digest = data.pop("intent_digest")
	del data["schema_version"], data["kind"]
	item = _intent(data | {"key": key})
	if digest != item.intent_digest:
		raise ValueError("persisted release intent digest differs")
	return item


@dataclass(frozen=True, slots=True)
class ReservationReleaseEvidence:
	name: str
	event_kind: str
	intent_digest: str
	reservation: str
	actor: str
	proof_hash: str
	session_digest: str
	recorded_at: str
	expires_at: str
	confirmation: str | None
	acknowledged: bool
	one_time_key: str | None
	event_digest: str


def _evidence(item, *, actor, proof_hash, session_digest, recorded_at, expires_at, confirmed=None):
	item = _require_intent(item)
	if normalize_reference(actor, label="actor") != actor:
		raise ValueError("canonical release actor required")
	proof = normalize_sha256(proof_hash, label="proof_hash")
	session = normalize_sha256(session_digest, label="session_digest")
	start, end = _time(recorded_at), _time(expires_at)
	if (
		not start >= _time(item.previous_occurred_at)
		or not 0 < (end - start).total_seconds() <= CONFIRMATION_SECONDS
	):
		raise ValueError("release confirmation time differs from history or policy")
	confirmation = None
	one_time = None
	if confirmed is not None:
		checked = verify_reservation_release_evidence(item, confirmed)
		if checked.event_kind != "Confirmation" or not _time(checked.recorded_at) <= start < end:
			raise ValueError("release decision is outside confirmation window")
		if (actor, proof, session, expires_at) != (
			checked.actor,
			checked.proof_hash,
			checked.session_digest,
			checked.expires_at,
		):
			raise ValueError("release decision differs from its confirmation")
		confirmation = checked.name
		one_time = fingerprint_json(
			{
				"namespace": "budget-reservation-release-successor-v1",
				"reservation": item.reservation,
				"previous_event_digest": item.previous_event_digest,
			}
		)
	content: dict[str, Any] = dict(
		event_kind="Decision" if confirmed is not None else "Confirmation",
		intent_digest=item.intent_digest,
		reservation=item.reservation,
		actor=actor,
		proof_hash=proof,
		session_digest=session,
		recorded_at=recorded_at,
		expires_at=expires_at,
		confirmation=confirmation,
		acknowledged=confirmed is not None,
		one_time_key=one_time,
	)
	name = fingerprint_json({"namespace": "budget-reservation-release-evidence-v1", **content})
	return ReservationReleaseEvidence(
		name=name, **content, event_digest=fingerprint_json({"name": name, **content})
	)


def reservation_release_confirmation(item, *, actor, proof_hash, session_digest, recorded_at, expires_at):
	return _evidence(
		item,
		actor=actor,
		proof_hash=proof_hash,
		session_digest=session_digest,
		recorded_at=recorded_at,
		expires_at=expires_at,
	)


def verify_reservation_release_evidence(item, value, confirmed=None):
	if type(value) is not ReservationReleaseEvidence:
		raise ValueError("canonical release evidence required")
	expected = _evidence(
		item,
		actor=value.actor,
		proof_hash=value.proof_hash,
		session_digest=value.session_digest,
		recorded_at=value.recorded_at,
		expires_at=value.expires_at,
		confirmed=confirmed,
	)
	if value != expected:
		raise ValueError("release evidence differs from confirmed intent")
	return expected


def verify_reservation_release_proof(item, confirmed, *, proof, actor, session_digest, now):
	checked = verify_reservation_release_evidence(item, confirmed)
	if (
		checked.event_kind != "Confirmation"
		or not isinstance(proof, str)
		or not 32 <= len(proof) <= 128
		or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in proof)
		or actor != checked.actor
		or not hmac.compare_digest(
			normalize_sha256(session_digest, label="session_digest"), checked.session_digest
		)
		or not hmac.compare_digest(hashlib.sha256(proof.encode("ascii")).hexdigest(), checked.proof_hash)
		or not _time(checked.recorded_at) <= _time(now) < _time(checked.expires_at)
	):
		raise ValueError("release proof, actor, session or time differs")


def reservation_release_decision(item, confirmed, *, proof, actor, session_digest, recorded_at, acknowledge):
	if acknowledge is not True:
		raise ValueError("explicit release acknowledgement required")
	verify_reservation_release_proof(
		item, confirmed, proof=proof, actor=actor, session_digest=session_digest, now=recorded_at
	)
	return _evidence(
		item,
		actor=actor,
		proof_hash=confirmed.proof_hash,
		session_digest=session_digest,
		recorded_at=recorded_at,
		expires_at=confirmed.expires_at,
		confirmed=confirmed,
	)


def reservation_release_record(item, confirmed, decision, previous, *, occurred_at):
	item = _require_intent(item)
	verify_reservation_release_evidence(item, decision, confirmed)
	if (
		type(previous) is not ReservationRecord
		or previous.event_digest != item.previous_event_digest
		or previous.event.sequence != item.previous_sequence
		or previous.event.reservation != item.reservation
		or previous.occurred_at != item.previous_occurred_at
		or previous.event.key != item.key
		or previous.event.currency != item.currency
		or previous.event.precision != item.precision
		or not _time(decision.recorded_at) <= _time(occurred_at) < _time(decision.expires_at)
	):
		raise ValueError("release event differs from its predecessor or decision window")
	event = ReservationEvent(
		reservation_event_key(item.reservation, item.previous_sequence + 1),
		item.reservation,
		item.previous_sequence + 1,
		item.key,
		item.currency,
		item.precision,
		"Release Reservation",
		item.amount,
	)
	source = replace(previous.source, action=RELEASE_ACTION_PREFIX + decision.name)
	return reservation_record(
		event,
		source,
		actor=decision.actor,
		occurred_at=occurred_at,
		previous_digest=item.previous_event_digest,
	)


def verify_released_reservation_records(original, records, releases):
	"""Verify supplied release pairs against every tail event in one pass.

	Initial authorization, immutable storage and physical completeness must be
	proved separately. A posted decision without an event is rejected here too.
	"""
	if not isinstance(releases, (tuple, list)):
		raise ValueError("complete release confirmation and decision pairs required")
	final = verify_reservation_records(records)
	if len(releases) != len(records) - 1:
		raise ValueError("release decisions and events differ")
	position = verify_reservation_records(records[:1])
	for previous, record, pair in zip(records[:-1], records[1:], releases, strict=True):
		if not isinstance(pair, (tuple, list)) or len(pair) != 2:
			raise ValueError("release confirmation and decision rows required")
		item, confirmed = verify_reservation_release_row(pair[0])
		checked, decision = verify_reservation_release_row(pair[1], confirmed)
		expected = reservation_release_intent(
			original, position, previous, amount=item.amount, reason=item.reason
		)
		if (
			item != checked
			or item != expected
			or record
			!= reservation_release_record(item, confirmed, decision, previous, occurred_at=record.occurred_at)
		):
			raise ValueError("release history differs from independent evidence")
		with localcontext() as context:
			context.prec = 48
			position = replace(
				position,
				last_sequence=record.event.sequence,
				released=exact_decimal_amount(
					Decimal(position.released) + Decimal(item.amount), item.precision
				),
				open_reservation=exact_decimal_amount(
					Decimal(position.open_reservation) - Decimal(item.amount), item.precision
				),
			)
	if position != final:
		raise ValueError("released history differs from complete event replay")
	return final


RELEASE_ROW_FIELDS = frozenset(ReservationReleaseEvidence.__dataclass_fields__) | {
	"evidence_key",
	"previous_sequence",
	"previous_event_digest",
	*KEY_FIELDS,
	"budget_key_digest",
	"intent_snapshot_json",
	"proof_version",
}


def reservation_release_row(item, value, confirmed=None):
	checked = verify_reservation_release_evidence(item, value, confirmed)
	return {
		**asdict(checked),
		"evidence_key": checked.name,
		"previous_sequence": item.previous_sequence,
		"previous_event_digest": item.previous_event_digest,
		**asdict(item.key),
		"budget_key_digest": item.key.identity,
		"intent_snapshot_json": reservation_release_snapshot(item),
		"acknowledged": int(checked.acknowledged),
		"proof_version": 1,
	}


def verify_reservation_release_row(value, confirmed=None):
	if not isinstance(value, dict) or set(value) != RELEASE_ROW_FIELDS:
		raise ValueError("complete release evidence row required")
	row = dict(value)
	if (
		type(row["proof_version"]) is not int
		or row["proof_version"] != 1
		or type(row["acknowledged"]) is not int
		or type(row["previous_sequence"]) is not int
	):
		raise ValueError("canonical release evidence version and acknowledgement required")
	for name in ("recorded_at", "expires_at"):
		stamp = row[name]
		if isinstance(stamp, datetime) and stamp.tzinfo is None:
			row[name] = stamp.isoformat(sep=" ", timespec="microseconds")
		elif isinstance(stamp, str):
			parsed = datetime.fromisoformat(stamp)
			if parsed.tzinfo is not None or stamp not in (
				parsed.isoformat(sep=" ", timespec="seconds"),
				parsed.isoformat(sep=" ", timespec="microseconds"),
			):
				raise ValueError("canonical persisted release time required")
			row[name] = parsed.isoformat(sep=" ", timespec="microseconds")
		else:
			raise ValueError("canonical persisted release time required")
	for name in ("confirmation", "one_time_key"):
		if row[name] == "":
			row[name] = None
	for name in ("period_start", "period_end"):
		if isinstance(row[name], datetime) or not isinstance(row[name], (str, date)):
			raise ValueError("canonical persisted release period required")
		if isinstance(row[name], date):
			row[name] = row[name].isoformat()
	if row["project"] == "":
		row["project"] = None
	item = parse_reservation_release_snapshot(row["intent_snapshot_json"])
	values = {name: row[name] for name in ReservationReleaseEvidence.__dataclass_fields__}
	values["acknowledged"] = bool(row["acknowledged"])
	evidence = ReservationReleaseEvidence(**values)
	if reservation_release_row(item, evidence, confirmed) != row:
		raise ValueError("stored release evidence differs from bound facts")
	return item, evidence
