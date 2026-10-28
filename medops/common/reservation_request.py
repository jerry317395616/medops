"""Bounded API request parsing for reservation confirmation intent."""

from __future__ import annotations

from dataclasses import dataclass, field

from medops.common.budget_control import budget_control_key
from medops.common.budget_planning import object_payload
from medops.common.budget_workflow import build_human_confirmation
from medops.common.confirmation_privacy import ConfirmationPayload, ConfirmationProof
from medops.common.domain_service import normalize_sha256
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


@dataclass(frozen=True, slots=True)
class ReservationPostCommand:
	confirmation: str
	decision: str
	proof: str = field(repr=False)
	acknowledge: bool = True

	def __post_init__(self):
		object.__setattr__(self, "proof", ConfirmationProof(self.proof))


def build_reservation_post(value: object) -> ReservationPostCommand:
	if isinstance(value, ConfirmationPayload):
		value = value.unwrap()
	data = object_payload(value, {"confirmation", "decision", "proof", "acknowledge"})
	if set(data) != {"confirmation", "decision", "proof", "acknowledge"}:
		raise ValueError("complete reservation post command required")
	confirmed = build_human_confirmation(
		{"confirmation": data["confirmation"], "proof": data["proof"], "acknowledge": data["acknowledge"]}
	)
	return ReservationPostCommand(
		confirmation=normalize_sha256(confirmed.confirmation, label="confirmation"),
		decision=normalize_sha256(data["decision"], label="decision"),
		proof=confirmed.proof,
		acknowledge=True,
	)


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
