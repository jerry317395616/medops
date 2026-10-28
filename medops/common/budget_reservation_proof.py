"""Deterministic identities and integrity for reservation-backed event chains."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from medops.common.budget_reservation_events import (
	MAX_RESERVATION_EVENTS,
	ReservationEvent,
	ReservationPosition,
	replay_reservation_events,
)
from medops.common.domain_service import fingerprint_json, normalize_sha256
from medops.common.organization import normalize_positive_integer, normalize_reference


@dataclass(frozen=True, slots=True)
class ReservationSource:
	doctype: str
	name: str
	revision: str
	line: str
	allocation: str
	action: str

	def __post_init__(self):
		for field, value in asdict(self).items():
			if normalize_reference(value, label=field) != value:
				raise ValueError("canonical source identity required")


@dataclass(frozen=True, slots=True)
class ReservationRecord:
	event: ReservationEvent
	source: ReservationSource
	actor: str
	occurred_at: str
	previous_digest: str | None
	sequence_key: str
	source_action_key: str
	event_digest: str
	renewal_decision: str | None = None
	renewal_digest: str | None = None


def reservation_event_key(reservation: object, sequence: object) -> str:
	name = normalize_reference(reservation, label="reservation")
	if name != reservation or type(sequence) is not int:
		raise ValueError("canonical reservation and sequence required")
	number = normalize_positive_integer(sequence, label="sequence")
	if number > MAX_RESERVATION_EVENTS:
		raise ValueError("reservation sequence exceeds limit")
	return fingerprint_json(
		{"namespace": "budget-reservation-event-v1", "reservation": name, "sequence": number}
	)


def reservation_source_action_key(source: ReservationSource) -> str:
	"""One source allocation action may not be reused in another reservation."""
	if type(source) is not ReservationSource:
		raise ValueError("reservation source required")
	return fingerprint_json({"namespace": "budget-reservation-source-action-v1", **asdict(source)})


def reservation_record(
	event: ReservationEvent,
	source: ReservationSource,
	*,
	actor: object,
	occurred_at: object,
	previous_digest: object,
	renewal_decision: object = None,
	renewal_digest: object = None,
) -> ReservationRecord:
	"""Construct deterministic evidence; callers must authenticate the source.

	The adapter must derive source line, allocation and action from a verified
	business operation, not trust an arbitrary public idempotency key. Names must
	be canonical database identities. No record is persisted or authorized here.
	"""
	if type(event) is not ReservationEvent or type(source) is not ReservationSource:
		raise ValueError("event and source required")
	if event.name != reservation_event_key(event.reservation, event.sequence):
		raise ValueError("event identity differs from reservation sequence")
	actor_name = normalize_reference(actor, label="actor")
	if actor_name != actor or not isinstance(occurred_at, str):
		raise ValueError("canonical actor and time required")
	try:
		stamp = datetime.fromisoformat(occurred_at)
	except ValueError as error:
		raise ValueError("canonical event time required") from error
	if stamp.tzinfo is not None or stamp.isoformat(sep=" ", timespec="microseconds") != occurred_at:
		raise ValueError("canonical naive event time required")
	if event.sequence == 1:
		if previous_digest is not None:
			raise ValueError("first event cannot have a predecessor")
		previous = None
	else:
		previous = normalize_sha256(previous_digest, label="previous_digest")
		if previous != previous_digest:
			raise ValueError("canonical predecessor digest required")
	sequence_key = fingerprint_json(
		{
			"namespace": "budget-reservation-sequence-v1",
			"reservation": event.reservation,
			"sequence": event.sequence,
		}
	)
	source_key = reservation_source_action_key(source)
	content = {
		"schema_version": 1,
		"event": asdict(event),
		"source": asdict(source),
		"actor": actor_name,
		"occurred_at": occurred_at,
		"previous_digest": previous,
		"sequence_key": sequence_key,
		"source_action_key": source_key,
	}
	renewal_name: str | None = None
	renewal_hash: str | None = None
	if (renewal_decision is None) != (renewal_digest is None):
		raise ValueError("complete renewal evidence reference required")
	if renewal_decision is not None:
		if event.sequence != 1 or event.kind != "Reserve":
			raise ValueError("renewal evidence is supported only for the first reservation")
		renewal_name = normalize_sha256(renewal_decision, label="renewal_decision")
		renewal_hash = normalize_sha256(renewal_digest, label="renewal_digest")
		content["schema_version"] = 2
		content["renewal_decision"] = renewal_name
		content["renewal_digest"] = renewal_hash
	return ReservationRecord(
		event,
		source,
		actor_name,
		occurred_at,
		previous,
		sequence_key,
		source_key,
		fingerprint_json(content),
		renewal_name,
		renewal_hash,
	)


def verify_reservation_records(value: object) -> ReservationPosition:
	"""Verify exact chain structure and replay; caller must prove DB completeness."""
	if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= MAX_RESERVATION_EVENTS:
		raise ValueError("bounded nonempty reservation records required")
	previous = None
	sources = set()
	events = []
	for record in value:
		if type(record) is not ReservationRecord or record.previous_digest != previous:
			raise ValueError("reservation predecessor mismatch")
		expected = reservation_record(
			record.event,
			record.source,
			actor=record.actor,
			occurred_at=record.occurred_at,
			previous_digest=record.previous_digest,
			renewal_decision=record.renewal_decision,
			renewal_digest=record.renewal_digest,
		)
		if record != expected or record.source_action_key in sources:
			raise ValueError("reservation evidence mismatch or duplicate source action")
		sources.add(record.source_action_key)
		previous = record.event_digest
		events.append(record.event)
	return replay_reservation_events(events)
