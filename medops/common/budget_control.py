"""Pure budget-key and exact arithmetic contracts; no reservation authority.

Callers must prove complete source events under the eventual database lock.
These functions do not read or write balances, grant permissions, or post funds.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext

from medops.common.budget_planning import object_payload
from medops.common.domain_service import fingerprint_json
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount, signed_decimal_amount
from medops.common.organization import normalize_positive_integer, normalize_reference

KEY_FIELDS = frozenset(
	{
		"company",
		"hospital",
		"organization_unit",
		"fiscal_year",
		"budget_subject",
		"funding_source",
		"project",
		"period_index",
	}
)
BALANCE_FIELDS = frozenset(
	{"approved", "adjustment_in", "adjustment_out", "reserved", "committed", "actual", "released"}
)


@dataclass(frozen=True, slots=True)
class BudgetControlKey:
	company: str
	hospital: str
	organization_unit: str
	fiscal_year: str
	budget_subject: str
	funding_source: str
	project: str | None
	period_index: int

	def __post_init__(self) -> None:
		for name in (
			"company",
			"hospital",
			"organization_unit",
			"fiscal_year",
			"budget_subject",
			"funding_source",
		):
			value = getattr(self, name)
			if normalize_reference(value, label=name) != value:
				raise ValueError("budget key must be canonical")
		if self.project is not None and normalize_reference(self.project, label="project") != self.project:
			raise ValueError("budget key must be canonical")
		if (
			type(self.period_index) is not int
			or normalize_positive_integer(self.period_index, label="period_index") != self.period_index
		):
			raise ValueError("budget key must be canonical")

	@property
	def identity(self) -> str:
		return fingerprint_json(asdict(self))


def budget_control_key(value: object) -> BudgetControlKey:
	"""Normalize every BR-BUD-002 dimension; absent project is explicit."""
	data = object_payload(value, set(KEY_FIELDS))
	if set(data) != KEY_FIELDS:
		raise ValueError("complete budget key required")
	return BudgetControlKey(
		company=normalize_reference(data["company"], label="company"),
		hospital=normalize_reference(data["hospital"], label="hospital"),
		organization_unit=normalize_reference(data["organization_unit"], label="organization_unit"),
		fiscal_year=normalize_reference(data["fiscal_year"], label="fiscal_year"),
		budget_subject=normalize_reference(data["budget_subject"], label="budget_subject"),
		funding_source=normalize_reference(data["funding_source"], label="funding_source"),
		project=(
			None if data["project"] in (None, "") else normalize_reference(data["project"], label="project")
		),
		period_index=normalize_positive_integer(data["period_index"], label="period_index"),
	)


def available_budget(value: object, precision: int) -> str:
	"""Calculate BR-BUD-001 from a complete, independently verified aggregate.

	A negative result remains visible for reconciliation and cannot authorize a
	new reservation. Completeness, event linkage, currency, and locking are the
	responsibility of the future transactional service, not this reducer.
	"""
	data = object_payload(value, set(BALANCE_FIELDS))
	if set(data) != BALANCE_FIELDS:
		raise ValueError("complete budget components required")
	with localcontext() as context:
		context.prec = 48
		amounts = {
			key: Decimal(exact_decimal_amount(data[key], precision, maximum=MAX_APPROVAL_AMOUNT))
			for key in BALANCE_FIELDS
		}
		amount = (
			amounts["approved"]
			+ amounts["adjustment_in"]
			- amounts["adjustment_out"]
			- amounts["reserved"]
			- amounts["committed"]
			- amounts["actual"]
			+ amounts["released"]
		)
		return signed_decimal_amount(amount, precision)


def fits_available_budget(available: object, requested: object, precision: int) -> bool:
	"""Compare exact values only; this does not create or authorize a hold."""
	with localcontext() as context:
		context.prec = 48
		balance = Decimal(signed_decimal_amount(available, precision))
		request = Decimal(exact_decimal_amount(requested, precision, maximum=MAX_APPROVAL_AMOUNT))
		if not request:
			raise ValueError("reservation amount must be positive")
		return balance >= request
