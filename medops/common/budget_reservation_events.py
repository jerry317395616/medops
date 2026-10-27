"""Exact reservation-backed event replay; no database or authority to write."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from medops.common.budget_control import BudgetControlKey
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.organization import normalize_positive_integer, normalize_reference

MAX_RESERVATION_EVENTS = 10000
EVENT_TYPES = frozenset({"Reserve", "Release Reservation", "Commit", "Release Commitment", "Actual"})


@dataclass(frozen=True, slots=True)
class ReservationEvent:
	name: str
	reservation: str
	sequence: int
	key: BudgetControlKey
	currency: str
	precision: int
	kind: str
	amount: str

	def __post_init__(self):
		if normalize_reference(self.name, label="event") != self.name:
			raise ValueError("canonical event identity required")
		if normalize_reference(self.reservation, label="reservation") != self.reservation:
			raise ValueError("canonical reservation identity required")
		if (
			type(self.sequence) is not int
			or normalize_positive_integer(self.sequence, label="sequence") != self.sequence
		):
			raise ValueError("positive integer sequence required")
		if type(self.key) is not BudgetControlKey:
			raise ValueError("canonical budget key required")
		if normalize_reference(self.currency, label="currency") != self.currency:
			raise ValueError("canonical currency required")
		if type(self.precision) is not int or not 0 <= self.precision <= 9:
			raise ValueError("captured currency precision required")
		if not isinstance(self.kind, str) or self.kind not in EVENT_TYPES:
			raise ValueError("unknown reservation event")
		canonical = exact_decimal_amount(self.amount, self.precision, maximum=MAX_APPROVAL_AMOUNT)
		if canonical != self.amount or Decimal(canonical) <= 0:
			raise ValueError("positive canonical amount required")


@dataclass(frozen=True, slots=True)
class ReservationPosition:
	reservation: str
	key: BudgetControlKey
	currency: str
	precision: int
	last_sequence: int
	reserved: str
	committed: str
	actual: str
	released: str
	open_reservation: str
	open_commitment: str


def replay_reservation_events(value: object) -> ReservationPosition:
	"""Replay one complete reservation-backed chain with exact currency amounts.

	The released component includes transfers out of an earlier state as well as
	actual releases. This preserves BR-BUD-001 without double-counting transfers.
	The caller must establish complete, authorized, immutable source events,
	database identities, deduplication, the shared guard, and budget sufficiency.
	This function neither persists an event nor authorizes a financial action.
	"""
	if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= MAX_RESERVATION_EVENTS:
		raise ValueError("bounded nonempty reservation events required")
	first = value[0]
	if type(first) is not ReservationEvent:
		raise ValueError("reservation events required")
	reserved = committed = actual = released = open_reservation = open_commitment = Decimal(0)
	seen = set()
	with localcontext() as context:
		context.prec = 48
		for sequence, event in enumerate(value, 1):
			if type(event) is not ReservationEvent or event.sequence != sequence or event.name in seen:
				raise ValueError("complete ordered unique event chain required")
			if (event.reservation, event.key, event.currency, event.precision) != (
				first.reservation,
				first.key,
				first.currency,
				first.precision,
			):
				raise ValueError("reservation event identity changed")
			seen.add(event.name)
			amount = Decimal(event.amount)
			if event.kind == "Reserve":
				reserved += amount
				open_reservation += amount
			elif event.kind in {"Release Reservation", "Commit"}:
				if amount > open_reservation:
					raise ValueError("reservation amount exceeded")
				open_reservation -= amount
				released += amount
				if event.kind == "Commit":
					committed += amount
					open_commitment += amount
			else:
				if amount > open_commitment:
					raise ValueError("commitment amount exceeded")
				open_commitment -= amount
				released += amount
				if event.kind == "Actual":
					actual += amount

		def amount_text(amount):
			return exact_decimal_amount(amount, first.precision, maximum=MAX_APPROVAL_AMOUNT)

		return ReservationPosition(
			first.reservation,
			first.key,
			first.currency,
			first.precision,
			len(value),
			amount_text(reserved),
			amount_text(committed),
			amount_text(actual),
			amount_text(released),
			amount_text(open_reservation),
			amount_text(open_commitment),
		)
