"""Match verified release facts to a purchase line without granting budget authority."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import TypedDict

from medops.common.budget_control import (
	BudgetControlKey,
	ordered_budget_keys,
	require_nonoverlapping_budget_periods,
)
from medops.common.budget_control_history import MAX_RELEASE_HISTORY_ENTRIES, ReleaseInventoryEntry
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.organization import normalize_reference, normalize_required_date


class PurchaseMatchInputs(TypedDict):
	company: str
	hospital: str
	organization_unit: str
	project: str | None
	account: str
	cost_center: str
	currency: str
	budget_date: str
	amount: str


@dataclass(frozen=True, slots=True)
class PurchaseBudgetCandidate:
	key: BudgetControlKey
	currency: str
	precision: int


def match_purchase_release_keys(
	keys: object,
	history: object,
	*,
	company: str,
	hospital: str,
	organization_unit: str,
	project: str | None,
	account: str,
	cost_center: str,
	currency: str,
	budget_date: str,
	amount: str,
) -> tuple[PurchaseBudgetCandidate, ...]:
	"""Find positively released keys matching a selected source line.

	Inputs must be derived from authorized persisted sources and complete history
	under the common guard. This function proves neither those properties nor
	available funds: reservation, commitment, actual and adjustment events are
	not included. The caller must separately decide which date and allocations
	are valid under the hospital's approved policy.
	"""
	requested = ordered_budget_keys(keys)
	for label, value in (
		("company", company),
		("hospital", hospital),
		("organization_unit", organization_unit),
		("account", account),
		("cost_center", cost_center),
		("currency", currency),
	):
		if normalize_reference(value, label=label) != value:
			raise ValueError("canonical purchase source identity required")
	if project is not None and normalize_reference(project, label="project") != project:
		raise ValueError("canonical purchase project required")
	if normalize_required_date(budget_date, label="budget_date") != budget_date:
		raise ValueError("canonical budget date required")
	if exact_decimal_amount(amount, 9, maximum=MAX_APPROVAL_AMOUNT) != amount or Decimal(amount) <= 0:
		raise ValueError("positive exact purchase amount required")
	if not isinstance(history, (tuple, list)) or len(history) > MAX_RELEASE_HISTORY_ENTRIES:
		raise ValueError("bounded complete release history required")
	for key in requested:
		if (key.company, key.hospital, key.organization_unit, key.project) != (
			company,
			hospital,
			organization_unit,
			project,
		) or not key.period_start <= budget_date <= key.period_end:
			raise ValueError("proposed budget key differs from source or date")
	requested_set = set(requested)
	scopes = {
		(
			key.company,
			key.hospital,
			key.organization_unit,
			key.budget_subject,
			key.funding_source,
			key.project,
		)
		for key in requested
	}
	all_keys = set(requested)
	names: set[str] = set()
	precisions: dict[BudgetControlKey, int] = {}
	totals: dict[BudgetControlKey, Decimal] = {}
	with localcontext() as context:
		context.prec = 48
		for entry in history:
			if type(entry) is not ReleaseInventoryEntry or entry.name in names:
				raise ValueError("unique verified release entries required")
			names.add(entry.name)
			key = entry.key
			if (
				key.company,
				key.hospital,
				key.organization_unit,
				key.budget_subject,
				key.funding_source,
				key.project,
			) not in scopes:
				raise ValueError("release history is outside requested scopes")
			all_keys.add(key)
			if key not in requested_set:
				continue
			if entry.currency != currency or (key in precisions and precisions[key] != entry.precision):
				raise ValueError("ambiguous release currency or precision")
			precisions[key] = entry.precision
			if entry.account == account and entry.cost_center == cost_center:
				totals[key] = totals.get(key, Decimal(0)) + Decimal(entry.amount)
		require_nonoverlapping_budget_periods(list(all_keys))
		result = []
		for key in requested:
			if totals.get(key, Decimal(0)) > 0:
				precision = precisions[key]
				exact_decimal_amount(amount, precision, maximum=MAX_APPROVAL_AMOUNT)
				result.append(PurchaseBudgetCandidate(key, currency, precision))
		return tuple(result)
