"""Pure, non-posting contract for renewing an expired reservation decision.

Storage, complete chain discovery, locking, session authentication and actual
human approval remain the responsibility of a future transaction service.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from medops.common.domain_service import fingerprint_json
from medops.common.reservation_authorization import (
	ReservationConfirmation,
	ReservationIntent,
	reservation_confirmation,
	verify_reservation_confirmation_proof,
	verify_reservation_decision_evidence,
)

MAX_RENEWALS = 100


@dataclass(frozen=True, slots=True)
class ReservationRenewalDecision:
	name: str
	original_decision: str
	previous_decision: str
	sequence: int
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


def _time(value: object) -> datetime:
	if not isinstance(value, str):
		raise ValueError("canonical renewal time required")
	try:
		result = datetime.fromisoformat(value)
	except ValueError as error:
		raise ValueError("canonical renewal time required") from error
	if result.tzinfo is not None or result.isoformat(sep=" ", timespec="microseconds") != value:
		raise ValueError("canonical renewal time required")
	return result


def _confirmation(item: ReservationIntent, value: object) -> ReservationConfirmation:
	if type(value) is not ReservationConfirmation:
		raise ValueError("canonical renewal confirmation required")
	checked = reservation_confirmation(
		item,
		actor=value.actor,
		proof_hash=value.proof_hash,
		session_digest=value.session_digest,
		recorded_at=value.recorded_at,
		expires_at=value.expires_at,
	)
	if checked != value:
		raise ValueError("renewal confirmation differs from intent")
	return checked


def _record(item, original, previous, sequence, confirmed, recorded_at):
	if _time(confirmed.recorded_at) < _time(previous.expires_at):
		raise ValueError("previous decision has not expired")
	if not _time(confirmed.recorded_at) <= _time(recorded_at) < _time(confirmed.expires_at):
		raise ValueError("renewal decision is outside its confirmation window")
	one_time_key = fingerprint_json(
		{"namespace": "budget-reservation-renewal-successor-v1", "previous_decision": previous.name}
	)
	content = {
		"schema_version": 1,
		"kind": "reservation_renewal_decision",
		"original_decision": original.name,
		"previous_decision": previous.name,
		"sequence": sequence,
		"reservation_identity": item.reservation_identity,
		"intent_digest": item.intent_digest,
		"source_snapshot_digest": item.source_snapshot_digest,
		"confirmation": confirmed.name,
		"actor": confirmed.actor,
		"proof_hash": confirmed.proof_hash,
		"session_digest": confirmed.session_digest,
		"recorded_at": recorded_at,
		"expires_at": confirmed.expires_at,
		"acknowledged": True,
		"one_time_key": one_time_key,
	}
	name = fingerprint_json({"namespace": "budget-reservation-renewal-event-v1", **content})
	return ReservationRenewalDecision(
		name=name,
		original_decision=original.name,
		previous_decision=previous.name,
		sequence=sequence,
		reservation_identity=item.reservation_identity,
		intent_digest=item.intent_digest,
		source_snapshot_digest=item.source_snapshot_digest,
		confirmation=confirmed.name,
		actor=confirmed.actor,
		proof_hash=confirmed.proof_hash,
		session_digest=confirmed.session_digest,
		recorded_at=recorded_at,
		expires_at=confirmed.expires_at,
		acknowledged=True,
		one_time_key=one_time_key,
		event_digest=fingerprint_json({"name": name, **content}),
	)


def verify_reservation_renewal_chain(item, first, original, renewals):
	"""Verify supplied lineage only; it cannot prove storage completeness or approval."""
	original = verify_reservation_decision_evidence(item, first, original)
	if not isinstance(renewals, (tuple, list)) or len(renewals) > MAX_RENEWALS:
		raise ValueError("bounded renewal evidence sequence required")
	previous = original
	for sequence, pair in enumerate(renewals, start=1):
		if not isinstance(pair, (tuple, list)) or len(pair) != 2:
			raise ValueError("renewal confirmation and decision required")
		confirmed, decision = pair
		confirmed = _confirmation(item, confirmed)
		if (
			type(decision) is not ReservationRenewalDecision
			or type(decision.sequence) is not int
			or decision.acknowledged is not True
		):
			raise ValueError("canonical renewal decision required")
		expected = _record(item, original, previous, sequence, confirmed, decision.recorded_at)
		if decision != expected:
			raise ValueError("renewal decision differs from lineage")
		previous = decision
	return previous


def reservation_renewal_decision(
	item, first, original, renewals, confirmed, *, proof, actor, session_digest, recorded_at, acknowledge
):
	"""Build a successor after explicit acknowledgement and proof validation.

	The caller must establish a complete locked chain, no posted reservation,
	a current authenticated session and permission; this alone grants no write.
	"""
	if acknowledge is not True:
		raise ValueError("explicit acknowledgement required")
	previous = verify_reservation_renewal_chain(item, first, original, renewals)
	if len(renewals) >= MAX_RENEWALS:
		raise ValueError("renewal sequence limit reached")
	confirmed = _confirmation(item, confirmed)
	verify_reservation_confirmation_proof(
		item, confirmed, proof=proof, actor=actor, session_digest=session_digest, now=recorded_at
	)
	return _record(item, original, previous, len(renewals) + 1, confirmed, recorded_at)


RENEWAL_ROW_FIELDS = frozenset(ReservationRenewalDecision.__dataclass_fields__) | {
	"evidence_key",
	"company",
	"hospital",
	"organization_unit",
	"budget_key_digest",
	"proof_version",
}


def reservation_renewal_row(item, first, original, renewals, confirmed, decision):
	"""Construct an exact proposed storage row without writing or granting authority."""
	if not isinstance(renewals, (tuple, list)):
		raise ValueError("bounded renewal evidence sequence required")
	checked = verify_reservation_renewal_chain(item, first, original, [*renewals, (confirmed, decision)])
	row = asdict(checked)
	row.update(
		evidence_key=checked.name,
		company=item.key.company,
		hospital=item.key.hospital,
		organization_unit=item.key.organization_unit,
		budget_key_digest=item.key.identity,
		proof_version=1,
	)
	row["acknowledged"] = 1
	return row


def verify_reservation_renewal_row(item, first, original, renewals, confirmed, value):
	"""Reconstruct exact supplied storage fields; callers must prove complete durable lineage."""
	if not isinstance(value, dict) or set(value) != RENEWAL_ROW_FIELDS:
		raise ValueError("complete renewal row required")
	row = dict(value)
	if type(row["proof_version"]) is not int or row["proof_version"] != 1:
		raise ValueError("unsupported renewal row version")
	if type(row["acknowledged"]) is not int or row["acknowledged"] != 1:
		raise ValueError("canonical renewal acknowledgement required")
	if type(row["sequence"]) is not int:
		raise ValueError("canonical renewal sequence required")
	for field in ("recorded_at", "expires_at"):
		value = row[field]
		if isinstance(value, datetime) and value.tzinfo is None:
			row[field] = value.isoformat(sep=" ", timespec="microseconds")
		elif isinstance(value, str):
			try:
				parsed = datetime.fromisoformat(value)
			except ValueError as error:
				raise ValueError("canonical stored renewal time required") from error
			if parsed.tzinfo is not None or value not in (
				parsed.isoformat(sep=" ", timespec="seconds"),
				parsed.isoformat(sep=" ", timespec="microseconds"),
			):
				raise ValueError("canonical stored renewal time required")
			row[field] = parsed.isoformat(sep=" ", timespec="microseconds")
		else:
			raise ValueError("canonical stored renewal time required")
	decision_values = {field: row[field] for field in ReservationRenewalDecision.__dataclass_fields__}
	decision_values["acknowledged"] = True
	decision = ReservationRenewalDecision(**decision_values)
	if reservation_renewal_row(item, first, original, renewals, confirmed, decision) != row:
		raise ValueError("stored renewal differs from checked lineage")
	return decision
