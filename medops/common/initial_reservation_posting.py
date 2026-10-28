"""Construct a first reservation and event from independently checked evidence.

This module does not write documents, authorize an actor or own a transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from medops.common.authorized_reservation_projection import verify_authorized_initial_reservation
from medops.common.budget_reservation_anchor import reservation_anchor
from medops.common.budget_reservation_events import ReservationEvent
from medops.common.budget_reservation_proof import (
	ReservationSource,
	reservation_event_key,
	reservation_record,
)
from medops.common.budget_reservation_storage import reservation_record_row
from medops.common.domain_service import fingerprint_json
from medops.common.material_source_snapshot import (
	build_snapshot_reservation_projection,
	parse_material_source_snapshot,
)
from medops.common.reservation_authorization import verify_reservation_authorization_rows


@dataclass(frozen=True, slots=True)
class InitialReservationDocuments:
	root: Mapping[str, object]
	event: Mapping[str, object]


def initial_reservation_documents(confirmation_row: object, decision_row: object, *, occurred_at: object):
	"""Build deterministic initial document fields; external checks remain mandatory.

	The caller must read controlled evidence, validate the active actor, verify the
	current source and permissions, acquire all common guards and establish budget
	availability. Constructed documents do not prove a human decision or grant
	permission to write; only the default-disabled internal protocol accepts them.
	"""
	item, decision = verify_reservation_authorization_rows(confirmation_row, decision_row)
	facts = parse_material_source_snapshot(item.source_snapshot, expected_digest=item.source_snapshot_digest)
	name = fingerprint_json(
		{"namespace": "budget-reservation-root-v1", "source_identity": item.reservation_identity}
	)
	source = ReservationSource(
		"Material Request",
		facts["request"],
		item.source_snapshot_digest,
		facts["line"],
		item.allocation,
		"Reserve",
	)
	event = ReservationEvent(
		reservation_event_key(name, 1),
		name,
		1,
		item.key,
		item.currency,
		item.precision,
		"Reserve",
		item.amount,
	)
	record = reservation_record(
		event, source, actor=decision.actor, occurred_at=occurred_at, previous_digest=None
	)
	anchor = reservation_anchor(
		[record],
		expected_source=source,
		source_snapshot_digest=item.source_snapshot_digest,
		cost_center=facts["cost_center"],
	)
	projection = build_snapshot_reservation_projection(
		anchor, [record], source_facts=facts, allocation=item.allocation, expected_key=item.key
	)
	event_row = reservation_record_row(record)
	verify_authorized_initial_reservation(
		projection, [event_row], confirmation_row, decision_row, current_facts=facts
	)
	root = {
		**projection,
		"status": "In Progress",
		"posting_date": item.budget_date,
		"applicant": decision.actor,
		"subject": "采购预算预占",
		"total_amount": item.amount,
		"revision": 1,
		"created_by": decision.actor,
	}
	return InitialReservationDocuments(MappingProxyType(root), MappingProxyType(event_row))
