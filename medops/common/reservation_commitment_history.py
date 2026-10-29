"""Bind every Commit/release to separate immutable evidence; no storage authority."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal, localcontext
from itertools import pairwise

from medops.common.budget_reservation_events import MAX_RESERVATION_EVENTS, ReservationEvent
from medops.common.budget_reservation_proof import (
	ReservationRecord,
	reservation_event_key,
	reservation_record,
	verify_reservation_records,
)
from medops.common.commitment_evidence import (
	commitment_sources,
	verify_commitment_evidence,
	verify_commitment_evidence_row,
)
from medops.common.exact_money import exact_decimal_amount
from medops.common.reservation_commitment import commitment_intent_for_position, require_commitment_intent
from medops.common.reservation_release import (
	reservation_release_intent,
	reservation_release_record,
	verify_reservation_release_row,
)

COMMIT_ACTION_PREFIX = "Commit:"


def commitment_record(item, confirmed, decision, previous, *, material_snapshot, order_snapshot, occurred_at):
	"""Build a lineage event bound to the independent PO evidence and decision.

	The event retains the original MR lineage. Its action identifies the exact
	independent commitment decision, whose immutable row binds the PO snapshot.
	Callers must establish complete history, current rights and all allocations.
	"""
	item = require_commitment_intent(item)
	verify_commitment_evidence(item, decision, confirmed)
	material_order = commitment_sources(item, material_snapshot, order_snapshot)
	try:
		stamp = datetime.fromisoformat(occurred_at)
	except (TypeError, ValueError) as error:
		raise ValueError("canonical commitment event time required") from error
	if (
		type(previous) is not ReservationRecord
		or previous.event_digest != item.previous_event_digest
		or previous.event.sequence != item.previous_sequence
		or previous.event.reservation != item.reservation
		or previous.occurred_at != item.previous_occurred_at
		or (previous.event.key, previous.event.currency, previous.event.precision)
		!= (item.key, item.currency, item.precision)
		or (previous.source.doctype, previous.source.name, previous.source.line, previous.source.revision)
		!= (
			"Material Request",
			material_order["material_request"],
			material_order["material_request_line"],
			item.original_source_digest,
		)
		or stamp.tzinfo is not None
		or stamp.isoformat(sep=" ", timespec="microseconds") != occurred_at
		or not datetime.fromisoformat(decision.recorded_at)
		<= stamp
		< datetime.fromisoformat(decision.expires_at)
	):
		raise ValueError("commitment event differs from its predecessor or independent decision")
	event = ReservationEvent(
		reservation_event_key(item.reservation, item.previous_sequence + 1),
		item.reservation,
		item.previous_sequence + 1,
		item.key,
		item.currency,
		item.precision,
		"Commit",
		item.amount,
	)
	return reservation_record(
		event,
		replace(previous.source, action=COMMIT_ACTION_PREFIX + decision.name),
		actor=decision.actor,
		occurred_at=occurred_at,
		previous_digest=item.previous_event_digest,
	)


def verify_reservation_action_records(original, records, releases, commitments):
	"""Verify all supplied independent pairs and every tail in one ordered replay.

	A digest is not authority: storage origin, physical completeness, initial
	authorization, current permissions and cross-root PO allocations remain the
	caller's responsibility. Unknown actions and orphan decisions are rejected.
	"""
	final = verify_reservation_records(records)
	if not isinstance(releases, (list, tuple)) or not isinstance(commitments, (list, tuple)):
		raise ValueError("complete release and commitment evidence sets required")
	if len(releases) + len(commitments) != len(records) - 1:
		raise ValueError("independent decisions and physical event tails differ")
	by_sequence = {}
	seen = set()
	for kind, pairs, verifier in (
		("Release Reservation", releases, verify_reservation_release_row),
		("Commit", commitments, verify_commitment_evidence_row),
	):
		for pair in pairs:
			if not isinstance(pair, (tuple, list)) or len(pair) != 2:
				raise ValueError("complete independent confirmation and decision rows required")
			item, confirmed = verifier(pair[0])
			checked, decision = verifier(pair[1], confirmed)
			if (
				item != checked
				or item.previous_sequence in by_sequence
				or confirmed.name in seen
				or decision.name in seen
			):
				raise ValueError("independent evidence cannot be reused or authorize two successors")
			seen.update((confirmed.name, decision.name))
			by_sequence[item.previous_sequence] = (kind, item, confirmed, decision, pair)
	position = verify_reservation_records(records[:1])
	for previous, record in pairwise(records):
		bound = by_sequence.get(previous.event.sequence)
		if bound is None or bound[0] != record.event.kind:
			raise ValueError("one matching independent decision required for every event")
		kind, item, confirmed, decision, pair = bound
		if kind == "Commit":
			expected = commitment_intent_for_position(
				original,
				position,
				previous,
				order_snapshot=pair[0]["order_snapshot_json"],
				order_digest=item.order_snapshot_digest,
				material_snapshot=pair[0]["material_snapshot_json"],
				material_digest=item.current_source_digest,
				amount=item.amount,
				reason=item.reason,
			)
			expected_record = commitment_record(
				item,
				confirmed,
				decision,
				previous,
				material_snapshot=pair[0]["material_snapshot_json"],
				order_snapshot=pair[0]["order_snapshot_json"],
				occurred_at=record.occurred_at,
			)
		else:
			expected = reservation_release_intent(
				original, position, previous, amount=item.amount, reason=item.reason
			)
			expected_record = reservation_release_record(
				item, confirmed, decision, previous, occurred_at=record.occurred_at
			)
		if item != expected or record != expected_record:
			raise ValueError("event history differs from independently confirmed intent")
		with localcontext() as context:
			context.prec = 48
			amount = Decimal(item.amount)
			position = replace(
				position,
				last_sequence=record.event.sequence,
				released=exact_decimal_amount(Decimal(position.released) + amount, item.precision),
				open_reservation=exact_decimal_amount(
					Decimal(position.open_reservation) - amount, item.precision
				),
				committed=exact_decimal_amount(
					Decimal(position.committed) + (amount if kind == "Commit" else 0), item.precision
				),
				open_commitment=exact_decimal_amount(
					Decimal(position.open_commitment) + (amount if kind == "Commit" else 0), item.precision
				),
			)
	if position != final or len(by_sequence) > MAX_RESERVATION_EVENTS - 1:
		raise ValueError("authorized history differs from complete replay")
	return final
