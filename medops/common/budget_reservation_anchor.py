"""Bind a reservation event chain to its initial source and budget dimensions.

This contract performs no database access and cannot establish authority,
source authenticity, complete history, sufficient funds or a trusted anchor.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime

from medops.common.budget_control import KEY_FIELDS
from medops.common.budget_reservation_events import MAX_RESERVATION_EVENTS, ReservationPosition
from medops.common.budget_reservation_proof import (
	ReservationRecord,
	ReservationSource,
	verify_reservation_records,
)
from medops.common.domain_service import fingerprint_json, normalize_sha256
from medops.common.organization import normalize_reference


def reservation_source_identity(source: ReservationSource) -> str:
	"""Identify a source allocation independently of its revision and action.

	The source adapter must supply the real allocation identity. This conservative
	identity prevents a source edit or action name alone creating a second root;
	hospital amendment and re-reservation policy remains to be implemented.
	"""
	if type(source) is not ReservationSource:
		raise ValueError("canonical reservation source required")
	return fingerprint_json(
		{
			"namespace": "budget-reservation-source-v1",
			"doctype": source.doctype,
			"name": source.name,
			"line": source.line,
			"allocation": source.allocation,
		}
	)


@dataclass(frozen=True, slots=True)
class ReservationAnchor:
	"""An untrusted projection derived from one supplied chain and source."""

	source: ReservationSource
	source_identity_key: str
	source_snapshot_digest: str
	cost_center: str
	position: ReservationPosition
	last_event_digest: str
	anchor_digest: str


def reservation_anchor(
	records: object,
	*,
	expected_source: ReservationSource,
	source_snapshot_digest: object,
	cost_center: object,
) -> ReservationAnchor:
	"""Reconstruct a deterministic anchor without trusting stored totals.

	The first event must correspond to the supplied initial source. The caller
	must independently read and authenticate that source and snapshot, acquire
	the common budget guard, and establish that the supplied chain is complete.
	The digest is not a signature and cannot establish those prerequisites.
	"""
	if type(expected_source) is not ReservationSource:
		raise ValueError("canonical reservation source required")
	snapshot = normalize_sha256(source_snapshot_digest, label="source_snapshot_digest")
	center = normalize_reference(cost_center, label="cost_center")
	if center != cost_center:
		raise ValueError("canonical cost center required")
	position = verify_reservation_records(records)
	if not isinstance(records, (tuple, list)):
		raise ValueError("reservation records required")
	first, last = records[0], records[-1]
	if (
		type(first) is not ReservationRecord
		or first.event.kind != "Reserve"
		or first.source != expected_source
	):
		raise ValueError("initial source differs from reservation chain")
	source_key = reservation_source_identity(expected_source)
	content = {
		"schema_version": 1,
		"source": asdict(expected_source),
		"source_identity_key": source_key,
		"source_snapshot_digest": snapshot,
		"cost_center": center,
		"position": asdict(position),
		"last_event_digest": last.event_digest,
	}
	return ReservationAnchor(
		expected_source, source_key, snapshot, center, position, last.event_digest, fingerprint_json(content)
	)


def verify_reservation_anchor(
	anchor: object,
	records: object,
	*,
	expected_source: ReservationSource,
	source_snapshot_digest: object,
	cost_center: object,
) -> ReservationPosition:
	"""Compare an independently obtained anchor with the reconstructed chain."""
	if type(anchor) is not ReservationAnchor or anchor != reservation_anchor(
		records,
		expected_source=expected_source,
		source_snapshot_digest=source_snapshot_digest,
		cost_center=cost_center,
	):
		raise ValueError("reservation anchor mismatch")
	return anchor.position


ANCHOR_ROW_FIELDS = frozenset(
	{
		"name",
		"reservation_key",
		"source_identity_key",
		"source_doctype",
		"source_name",
		"source_revision",
		"source_line",
		"source_allocation",
		"source_snapshot_digest",
		"cost_center",
		"currency",
		"currency_precision",
		"event_count",
		"last_event_digest",
		"anchor_digest",
		"proof_version",
		"reserved_amount",
		"committed_amount",
		"actual_amount",
		*KEY_FIELDS,
	}
)


def reservation_anchor_row(anchor: ReservationAnchor) -> dict[str, object]:
	"""Project the verified amounts into selected aggregate columns, without writing."""
	if type(anchor) is not ReservationAnchor:
		raise ValueError("reservation anchor required")
	position, source = anchor.position, anchor.source
	return {
		"name": position.reservation,
		"reservation_key": position.reservation,
		"source_identity_key": anchor.source_identity_key,
		"source_doctype": source.doctype,
		"source_name": source.name,
		"source_revision": source.revision,
		"source_line": source.line,
		"source_allocation": source.allocation,
		"source_snapshot_digest": anchor.source_snapshot_digest,
		"cost_center": anchor.cost_center,
		**asdict(position.key),
		"currency": position.currency,
		"currency_precision": position.precision,
		"event_count": position.last_sequence,
		"last_event_digest": anchor.last_event_digest,
		"anchor_digest": anchor.anchor_digest,
		"proof_version": 1,
		"reserved_amount": position.open_reservation,
		"committed_amount": position.open_commitment,
		"actual_amount": position.actual,
	}


def verify_reservation_anchor_row(
	row: object,
	record_rows: object,
	*,
	expected_source: ReservationSource,
	source_snapshot_digest: object,
	cost_center: object,
) -> ReservationPosition:
	"""Compare a selected aggregate and event rows to independent source facts.

	This does not query or lock data. A caller must prove the aggregate was
	created by an authorized transaction and that *all* related events were read
	under the shared guard. Other aggregate columns are outside this projection.
	"""
	from medops.common.budget_reservation_storage import reservation_record_from_row

	if not isinstance(row, Mapping) or set(row) != ANCHOR_ROW_FIELDS:
		raise ValueError("complete reservation anchor projection required")
	if (
		type(row["event_count"]) is not int
		or not 1 <= row["event_count"] <= MAX_RESERVATION_EVENTS
		or type(row["currency_precision"]) is not int
		or type(row["proof_version"]) is not int
		or not isinstance(record_rows, (tuple, list))
		or len(record_rows) != row["event_count"]
	):
		raise ValueError("bounded reservation event rows required")
	records = tuple(reservation_record_from_row(value) for value in record_rows)
	anchor = reservation_anchor(
		records,
		expected_source=expected_source,
		source_snapshot_digest=source_snapshot_digest,
		cost_center=cost_center,
	)
	actual = dict(row)
	for field in ("period_start", "period_end"):
		value = actual[field]
		if isinstance(value, datetime) or not isinstance(value, (str, date)):
			raise ValueError("canonical reservation period required")
		if isinstance(value, date):
			actual[field] = value.isoformat()
	if actual["project"] == "":
		actual["project"] = None
	if actual != reservation_anchor_row(anchor):
		raise ValueError("reservation aggregate differs from event chain")
	return anchor.position


def verify_frozen_reservation_anchor_row(
	row, record_rows, *, expected_source, source_snapshot_digest, cost_center
) -> ReservationPosition:
	"""Check a frozen first-event anchor and structurally replay release tails.

	This is not release authorization. The caller must discover all physical
	events and independently validate every posted release decision and its
	confirmation under the shared guard before using the resulting position.
	"""
	from medops.common.budget_reservation_storage import reservation_record_from_row
	from medops.common.reservation_release import RELEASE_ACTION_PREFIX

	if not isinstance(record_rows, (tuple, list)) or not 1 <= len(record_rows) <= MAX_RESERVATION_EVENTS:
		raise ValueError("complete bounded frozen reservation history required")
	verify_reservation_anchor_row(
		row,
		record_rows[:1],
		expected_source=expected_source,
		source_snapshot_digest=source_snapshot_digest,
		cost_center=cost_center,
	)
	records = [reservation_record_from_row(value) for value in record_rows]
	for record in records[1:]:
		if (
			record.event.kind != "Release Reservation"
			or not record.source.action.startswith(RELEASE_ACTION_PREFIX)
			or record.source.action
			!= RELEASE_ACTION_PREFIX
			+ normalize_sha256(record.source.action[len(RELEASE_ACTION_PREFIX) :], label="release_decision")
			or (
				record.source.doctype,
				record.source.name,
				record.source.revision,
				record.source.line,
				record.source.allocation,
			)
			!= (
				expected_source.doctype,
				expected_source.name,
				expected_source.revision,
				expected_source.line,
				expected_source.allocation,
			)
		):
			raise ValueError("only bound release tails may extend the frozen initial root")
	return verify_reservation_records(records)
