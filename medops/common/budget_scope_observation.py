"""Exact arithmetic over supplied budget facts, never spending authority."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from medops.common.budget_control import BudgetControlKey
from medops.common.budget_control_history import MAX_RELEASE_HISTORY_ENTRIES, ReleaseInventoryEntry
from medops.common.budget_reservation_events import ReservationPosition
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount, signed_decimal_amount
from medops.common.organization import normalize_reference

MAX_OBSERVED_POSITIONS = 10000


@dataclass(frozen=True, slots=True)
class BudgetScopeObservation:
	key: BudgetControlKey
	currency: str
	precision: int
	net_releases: str
	exposure: str
	difference: str


def observe_budget_scope(key: object, currency: object, precision: object, history: object, usage: object):
	"""Calculate a diagnostic from supplied rows; it is not a spending balance.

	The caller must independently establish scope, permissions, source authority,
	complete event inventories and a shared transaction guard. This diagnostic
	does not establish policy, historic date semantics or writer completeness.
	"""
	if type(key) is not BudgetControlKey:
		raise ValueError("canonical budget key required")
	unit = normalize_reference(currency, label="currency")
	if unit != currency:
		raise ValueError("canonical currency required")
	if type(precision) is not int or not 0 <= precision <= 9:
		raise ValueError("captured currency precision required")
	if not isinstance(history, (tuple, list)) or len(history) > MAX_RELEASE_HISTORY_ENTRIES:
		raise ValueError("bounded release inventory required")
	if not isinstance(usage, (tuple, list)) or len(usage) > MAX_OBSERVED_POSITIONS:
		raise ValueError("bounded reservation positions required")
	with localcontext() as context:
		context.prec = 48
		releases = Decimal(0)
		exposure = Decimal(0)
		names = set()
		for entry in history:
			if type(entry) is not ReleaseInventoryEntry or entry.name in names:
				raise ValueError("unique release inventory required")
			names.add(entry.name)
			if entry.key == key:
				if (entry.currency, entry.precision) != (currency, precision):
					raise ValueError("release currency or precision differs")
				releases += Decimal(entry.amount)
		names.clear()
		for position in usage:
			if type(position) is not ReservationPosition or position.reservation in names:
				raise ValueError("unique reservation positions required")
			names.add(position.reservation)
			if position.key == key:
				if (position.currency, position.precision) != (currency, precision):
					raise ValueError("reservation currency or precision differs")
				for value in (position.open_reservation, position.open_commitment, position.actual):
					if exact_decimal_amount(value, precision, maximum=MAX_APPROVAL_AMOUNT) != value:
						raise ValueError("canonical observed usage required")
					exposure += Decimal(value)
		return BudgetScopeObservation(
			key,
			unit,
			precision,
			signed_decimal_amount(releases, precision),
			exact_decimal_amount(exposure, precision, maximum=MAX_APPROVAL_AMOUNT),
			signed_decimal_amount(releases - exposure, precision),
		)
