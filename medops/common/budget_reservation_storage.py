"""Strict database-row translation; no database access or write authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import date, datetime

from medops.common.budget_control import KEY_FIELDS, budget_control_key
from medops.common.budget_reservation_events import (
	MAX_RESERVATION_EVENTS,
	ReservationEvent,
	ReservationPosition,
)
from medops.common.budget_reservation_proof import (
	ReservationRecord,
	ReservationSource,
	reservation_record,
	verify_reservation_records,
)
from medops.common.domain_service import normalize_sha256

ROW_FIELDS = frozenset(
	{
		"name",
		"event_key",
		"reservation",
		"sequence",
		"sequence_key",
		"source_action_key",
		"currency",
		"currency_precision",
		"kind",
		"amount",
		"source_doctype",
		"source_name",
		"source_revision",
		"source_line",
		"source_allocation",
		"source_action",
		"actor",
		"occurred_at",
		"previous_digest",
		"event_digest",
		"proof_version",
		"renewal_decision",
		"renewal_digest",
		*KEY_FIELDS,
	}
)


def reservation_record_row(record: ReservationRecord) -> dict[str, object]:
	"""Serialize verified evidence without persisting or authorizing a write."""
	if type(record) is not ReservationRecord or record != reservation_record(
		record.event,
		record.source,
		actor=record.actor,
		occurred_at=record.occurred_at,
		previous_digest=record.previous_digest,
		renewal_decision=record.renewal_decision,
		renewal_digest=record.renewal_digest,
	):
		raise ValueError("invalid reservation record")
	event, source = record.event, record.source
	return {
		"name": event.name,
		"event_key": event.name,
		"reservation": event.reservation,
		"sequence": event.sequence,
		"sequence_key": record.sequence_key,
		"source_action_key": record.source_action_key,
		**asdict(event.key),
		"currency": event.currency,
		"currency_precision": event.precision,
		"kind": event.kind,
		"amount": event.amount,
		"source_doctype": source.doctype,
		"source_name": source.name,
		"source_revision": source.revision,
		"source_line": source.line,
		"source_allocation": source.allocation,
		"source_action": source.action,
		"actor": record.actor,
		"occurred_at": record.occurred_at,
		"previous_digest": record.previous_digest,
		"event_digest": record.event_digest,
		"proof_version": 2 if record.renewal_decision is not None else 1,
		"renewal_decision": record.renewal_decision,
		"renewal_digest": record.renewal_digest,
	}


def reservation_record_from_row(value: object) -> ReservationRecord:
	"""Reconstruct and check a selected row; this does not check its source rights."""
	if not isinstance(value, Mapping) or set(value) != ROW_FIELDS:
		raise ValueError("complete reservation event row required")
	if type(value["proof_version"]) is not int or value["proof_version"] not in (1, 2):
		raise ValueError("unsupported reservation proof version")
	renewal_decision = None if value["renewal_decision"] in (None, "") else value["renewal_decision"]
	renewal_digest = None if value["renewal_digest"] in (None, "") else value["renewal_digest"]
	if (value["proof_version"] == 1 and (renewal_decision is not None or renewal_digest is not None)) or (
		value["proof_version"] == 2 and (renewal_decision is None or renewal_digest is None)
	):
		raise ValueError("reservation proof version and renewal reference differ")
	if value["name"] != value["event_key"]:
		raise ValueError("reservation event row identity mismatch")
	values = {field: value[field] for field in KEY_FIELDS}
	for field in ("period_start", "period_end"):
		if isinstance(values[field], datetime) or not isinstance(values[field], (str, date)):
			raise ValueError("canonical reservation period required")
	key = budget_control_key(values)
	canonical = asdict(key)
	for field in KEY_FIELDS - {"project", "period_start", "period_end"}:
		if value[field] != canonical[field]:
			raise ValueError("noncanonical persisted budget identity")
	for field in ("period_start", "period_end"):
		persisted = value[field].isoformat() if isinstance(value[field], date) else value[field]
		if persisted != canonical[field]:
			raise ValueError("noncanonical persisted budget period")
	if value["project"] not in (None, "") and value["project"] != key.project:
		raise ValueError("noncanonical persisted project")
	event = ReservationEvent(
		value["name"],
		value["reservation"],
		value["sequence"],
		key,
		value["currency"],
		value["currency_precision"],
		value["kind"],
		value["amount"],
	)
	source = ReservationSource(
		value["source_doctype"],
		value["source_name"],
		value["source_revision"],
		value["source_line"],
		value["source_allocation"],
		value["source_action"],
	)
	stamp = value["occurred_at"]
	if isinstance(stamp, str):
		raw_stamp = stamp
		try:
			stamp = datetime.fromisoformat(stamp)
		except ValueError as error:
			raise ValueError("invalid persisted event time") from error
		canonical_times = {stamp.isoformat(sep=" ", timespec="microseconds")}
		if not stamp.microsecond:
			canonical_times.add(stamp.isoformat(sep=" ", timespec="seconds"))
		if raw_stamp not in canonical_times:
			raise ValueError("noncanonical persisted event time")
	if not isinstance(stamp, datetime) or stamp.tzinfo is not None:
		raise ValueError("invalid persisted event time")
	previous = None if value["previous_digest"] in (None, "") else value["previous_digest"]
	expected = reservation_record(
		event,
		source,
		actor=value["actor"],
		occurred_at=stamp.isoformat(sep=" ", timespec="microseconds"),
		previous_digest=previous,
		renewal_decision=renewal_decision,
		renewal_digest=renewal_digest,
	)
	if (
		value["sequence_key"] != expected.sequence_key
		or value["source_action_key"] != expected.source_action_key
		or value["event_digest"] != expected.event_digest
	):
		raise ValueError("persisted reservation evidence mismatch")
	return expected


def verify_reservation_rows(
	value: object, *, expected_reservation: object, expected_count: object, expected_digest: object
) -> ReservationPosition:
	"""Verify rows against an independently established count and final digest.

	An authorized service must obtain the anchor from a trusted, locked aggregate,
	query all matching rows and verify source permissions. This pure adapter does
	none of those things and cannot prove omitted rows or authorize a transaction.
	"""
	if (
		not isinstance(value, (tuple, list))
		or type(expected_count) is not int
		or not 1 <= expected_count <= MAX_RESERVATION_EVENTS
	):
		raise ValueError("reservation row count and rows required")
	if len(value) != expected_count:
		raise ValueError("reservation row count mismatch")
	digest = normalize_sha256(expected_digest, label="expected_digest")
	records = tuple(reservation_record_from_row(row) for row in value)
	position = verify_reservation_records(records)
	if position.reservation != expected_reservation or records[-1].event_digest != digest:
		raise ValueError("reservation chain anchor mismatch")
	return position
