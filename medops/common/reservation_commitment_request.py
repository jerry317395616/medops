"""Bounded commitment commands; snapshots and budget scopes never come from callers."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from medops.common.budget_planning import object_payload
from medops.common.budget_workflow import build_human_confirmation
from medops.common.confirmation_privacy import ConfirmationPayload, ConfirmationProof
from medops.common.domain_service import normalize_sha256
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.organization import normalize_reference, normalize_required_text


@dataclass(frozen=True, slots=True)
class CommitmentPrepare:
	reservation: str
	purchase_order: str
	purchase_order_line: str
	amount: str
	reason: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CommitmentPost:
	confirmation: str
	proof: str = field(repr=False)
	acknowledge: bool = True

	def __post_init__(self):
		object.__setattr__(self, "proof", ConfirmationProof(self.proof))


def build_commitment_prepare(value):
	fields = {"reservation", "purchase_order", "purchase_order_line", "amount", "reason"}
	data = object_payload(value, fields)
	if set(data) != fields:
		raise ValueError("complete commitment preview command required")
	amount = exact_decimal_amount(data["amount"], maximum=MAX_APPROVAL_AMOUNT)
	if Decimal(amount) <= 0:
		raise ValueError("positive commitment amount required")
	return CommitmentPrepare(
		normalize_sha256(data["reservation"], label="reservation"),
		normalize_reference(data["purchase_order"], label="purchase_order"),
		normalize_reference(data["purchase_order_line"], label="purchase_order_line"),
		amount,
		normalize_required_text(data["reason"], label="reason", maximum=1000),
	)


def build_commitment_post(value):
	if isinstance(value, ConfirmationPayload):
		value = value.unwrap()
	data = object_payload(value, {"confirmation", "proof", "acknowledge"})
	if set(data) != {"confirmation", "proof", "acknowledge"}:
		raise ValueError("complete commitment post command required")
	human = build_human_confirmation(data)
	return CommitmentPost(normalize_sha256(human.confirmation, label="confirmation"), human.proof, True)
