"""Exact private request contract for a reservation decision renewal."""

from __future__ import annotations

from dataclasses import dataclass, field

from medops.common.budget_planning import object_payload
from medops.common.budget_workflow import build_human_confirmation
from medops.common.confirmation_privacy import ConfirmationPayload, ConfirmationProof
from medops.common.domain_service import normalize_sha256


@dataclass(frozen=True, slots=True)
class RenewalCommand:
	original_confirmation: str
	original_decision: str
	renewal_confirmation: str
	proof: str = field(repr=False)
	acknowledge: bool = True

	def __post_init__(self):
		object.__setattr__(self, "proof", ConfirmationProof(self.proof))


def build_renewal_command(value: object) -> RenewalCommand:
	if isinstance(value, ConfirmationPayload):
		value = value.unwrap()
	fields = {"original_confirmation", "original_decision", "renewal_confirmation", "proof", "acknowledge"}
	data = object_payload(value, fields)
	if set(data) != fields:
		raise ValueError("complete renewal command required")
	human = build_human_confirmation(
		{
			"confirmation": data["renewal_confirmation"],
			"proof": data["proof"],
			"acknowledge": data["acknowledge"],
		}
	)
	return RenewalCommand(
		normalize_sha256(data["original_confirmation"], label="original_confirmation"),
		normalize_sha256(data["original_decision"], label="original_decision"),
		normalize_sha256(human.confirmation, label="renewal_confirmation"),
		human.proof,
		True,
	)


@dataclass(frozen=True, slots=True)
class RenewedReservationPostCommand:
	original_confirmation: str
	original_decision: str
	renewal_confirmation: str
	renewal_decision: str
	proof: str = field(repr=False)
	acknowledge: bool = True

	def __post_init__(self):
		object.__setattr__(self, "proof", ConfirmationProof(self.proof))


def build_renewed_reservation_post(value: object) -> RenewedReservationPostCommand:
	if isinstance(value, ConfirmationPayload):
		value = value.unwrap()
	fields = {
		"original_confirmation",
		"original_decision",
		"renewal_confirmation",
		"renewal_decision",
		"proof",
		"acknowledge",
	}
	data = object_payload(value, fields)
	if set(data) != fields:
		raise ValueError("complete renewed reservation command required")
	base = build_renewal_command({key: data[key] for key in fields - {"renewal_decision"}})
	return RenewedReservationPostCommand(
		base.original_confirmation,
		base.original_decision,
		base.renewal_confirmation,
		normalize_sha256(data["renewal_decision"], label="renewal_decision"),
		base.proof,
		True,
	)
