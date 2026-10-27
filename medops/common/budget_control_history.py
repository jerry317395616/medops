"""Validate release history compatibility without granting budget authority."""

from __future__ import annotations

from dataclasses import dataclass

from medops.common.budget_control import (
	BudgetControlKey,
	ordered_budget_keys,
	require_nonoverlapping_budget_periods,
)
from medops.common.exact_money import signed_decimal_amount
from medops.common.organization import normalize_reference

MAX_RELEASE_HISTORY_ENTRIES = 50000


@dataclass(frozen=True, slots=True)
class ReleaseInventoryEntry:
	name: str
	key: BudgetControlKey
	currency: str
	precision: int
	amount: str

	def __post_init__(self):
		if normalize_reference(self.name, label="entry") != self.name:
			raise ValueError("canonical entry name required")
		if type(self.key) is not BudgetControlKey:
			raise ValueError("canonical budget key required")
		if normalize_reference(self.currency, label="currency") != self.currency:
			raise ValueError("canonical currency required")
		if signed_decimal_amount(self.amount, self.precision) != self.amount:
			raise ValueError("canonical amount required")


def _scope(key: BudgetControlKey):
	return (
		key.company,
		key.hospital,
		key.organization_unit,
		key.budget_subject,
		key.funding_source,
		key.project,
	)


def require_compatible_release_history(
	proposed: object, history: object, *, currency: object, precision: int
) -> None:
	"""Reject ambiguous periods or inconsistent currency at the same key.

	This only checks the provided immutable facts. Callers must prove their
	completeness, database-canonical identities, permissions and shared locks.
	It does not calculate a balance, approve a reversal or authorize any write.
	"""
	keys = ordered_budget_keys(proposed)
	unit_currency = normalize_reference(currency, label="currency")
	if unit_currency != currency:
		raise ValueError("canonical currency required")
	signed_decimal_amount("0", precision)
	if not isinstance(history, (tuple, list)) or len(history) > MAX_RELEASE_HISTORY_ENTRIES:
		raise ValueError("bounded release history required")
	scopes = {_scope(key) for key in keys}
	proposed_set = set(keys)
	all_keys = set(keys)
	names = set()
	for entry in history:
		if type(entry) is not ReleaseInventoryEntry or entry.name in names:
			raise ValueError("unique release inventory entries required")
		names.add(entry.name)
		if _scope(entry.key) not in scopes:
			raise ValueError("history outside guarded scopes")
		all_keys.add(entry.key)
		if entry.key in proposed_set and (entry.currency != unit_currency or entry.precision != precision):
			raise ValueError("incompatible currency or precision")
	require_nonoverlapping_budget_periods(list(all_keys))
