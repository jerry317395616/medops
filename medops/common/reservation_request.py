"""Bounded API request parsing for reservation confirmation intent."""

from __future__ import annotations

from medops.common.budget_control import budget_control_key
from medops.common.budget_planning import object_payload
from medops.common.reservation_authorization import ReservationIntent, reservation_intent

PREPARE_FIELDS = {
	"source_snapshot",
	"source_snapshot_digest",
	"allocation",
	"budget_key",
	"currency",
	"precision",
	"amount",
	"budget_date",
	"reason",
}


def build_reservation_prepare(value: object) -> ReservationIntent:
	data = object_payload(value, PREPARE_FIELDS)
	if set(data) != PREPARE_FIELDS:
		raise ValueError("complete reservation confirmation intent required")
	return reservation_intent(
		source_snapshot=data["source_snapshot"],
		source_snapshot_digest=data["source_snapshot_digest"],
		allocation=data["allocation"],
		key=budget_control_key(data["budget_key"]),
		currency=data["currency"],
		precision=data["precision"],
		amount=data["amount"],
		budget_date=data["budget_date"],
		reason=data["reason"],
	)
