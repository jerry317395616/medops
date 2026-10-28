"""Exact arithmetic over supplied budget facts, never spending authority."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from medops.common.budget_control import MAX_CONTROL_KEYS, BudgetControlKey
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


def observe_budget_scopes(specifications: object, history: object, usage: object):
	"""Calculate bounded diagnostics in one pass; these are not spending balances.

	The caller must independently establish scope, permissions, source authority,
	complete event inventories and a shared transaction guard. This diagnostic
	does not establish policy, historic date semantics or writer completeness.
	"""
	if not isinstance(specifications, (tuple, list)) or not 1 <= len(specifications) <= MAX_CONTROL_KEYS:
		raise ValueError("bounded budget specifications required")
	specs: dict[BudgetControlKey, tuple[str, int]] = {}
	for spec in specifications:
		if not isinstance(spec, (tuple, list)) or len(spec) != 3:
			raise ValueError("complete budget specification required")
		key, currency, precision = spec
		if type(key) is not BudgetControlKey or key in specs:
			raise ValueError("unique canonical budget keys required")
		unit = normalize_reference(currency, label="currency")
		if unit != currency:
			raise ValueError("canonical currency required")
		if type(precision) is not int or not 0 <= precision <= 9:
			raise ValueError("captured currency precision required")
		specs[key] = (unit, precision)
	if not isinstance(history, (tuple, list)) or len(history) > MAX_RELEASE_HISTORY_ENTRIES:
		raise ValueError("bounded release inventory required")
	if not isinstance(usage, (tuple, list)) or len(usage) > MAX_OBSERVED_POSITIONS:
		raise ValueError("bounded reservation positions required")
	with localcontext() as context:
		context.prec = 48
		releases = dict.fromkeys(specs, Decimal(0))
		exposure = dict.fromkeys(specs, Decimal(0))
		names = set()
		for entry in history:
			if type(entry) is not ReleaseInventoryEntry or entry.name in names:
				raise ValueError("unique release inventory required")
			names.add(entry.name)
			if entry.key in specs:
				if (entry.currency, entry.precision) != specs[entry.key]:
					raise ValueError("release currency or precision differs")
				releases[entry.key] += Decimal(entry.amount)
		names.clear()
		for position in usage:
			if (
				type(position) is not ReservationPosition
				or type(position.key) is not BudgetControlKey
				or normalize_reference(position.reservation, label="reservation") != position.reservation
				or position.reservation in names
			):
				raise ValueError("unique reservation positions required")
			names.add(position.reservation)
			if position.key in specs:
				if (position.currency, position.precision) != specs[position.key]:
					raise ValueError("reservation currency or precision differs")
				precision = specs[position.key][1]
				for value in (position.open_reservation, position.open_commitment, position.actual):
					if exact_decimal_amount(value, precision, maximum=MAX_APPROVAL_AMOUNT) != value:
						raise ValueError("canonical observed usage required")
					exposure[position.key] += Decimal(value)
		return tuple(
			BudgetScopeObservation(
				key,
				unit,
				precision,
				signed_decimal_amount(releases[key], precision),
				exact_decimal_amount(exposure[key], precision, maximum=MAX_APPROVAL_AMOUNT),
				signed_decimal_amount(releases[key] - exposure[key], precision),
			)
			for key, (unit, precision) in specs.items()
		)


def observe_budget_scope(key: object, currency: object, precision: object, history: object, usage: object):
	"""Single-key convenience wrapper around the bounded diagnostic."""
	return observe_budget_scopes([(key, currency, precision)], history, usage)[0]
