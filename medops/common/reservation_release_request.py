"""Bounded private release preview and explicitly acknowledged post commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from medops.common.budget_planning import object_payload
from medops.common.budget_workflow import build_human_confirmation
from medops.common.confirmation_privacy import ConfirmationPayload, ConfirmationProof
from medops.common.domain_service import normalize_sha256
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.organization import normalize_required_text


@dataclass(frozen=True, slots=True)
class ReleaseReservationPrepare:
	reservation: str
	amount: str
	reason: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReleaseReservationPost:
	confirmation: str
	proof: str = field(repr=False)
	acknowledge: bool = True

	def __post_init__(self):
		object.__setattr__(self, "proof", ConfirmationProof(self.proof))


def build_release_reservation_prepare(value):
	data = object_payload(value, {"reservation", "amount", "reason"})
	if set(data) != {"reservation", "amount", "reason"}:
		raise ValueError("complete release preview command required")
	amount = exact_decimal_amount(data["amount"], maximum=MAX_APPROVAL_AMOUNT)
	if Decimal(amount) <= 0:
		raise ValueError("positive release amount required")
	return ReleaseReservationPrepare(
		normalize_sha256(data["reservation"], label="reservation"),
		amount,
		normalize_required_text(data["reason"], label="reason", maximum=1000),
	)


def build_release_reservation_post(value):
	if isinstance(value, ConfirmationPayload):
		value = value.unwrap()
	data = object_payload(value, {"confirmation", "proof", "acknowledge"})
	if set(data) != {"confirmation", "proof", "acknowledge"}:
		raise ValueError("complete release post command required")
	human = build_human_confirmation(data)
	return ReleaseReservationPost(
		normalize_sha256(human.confirmation, label="confirmation"), human.proof, True
	)
