"""Pure budget-key and exact arithmetic contracts; no reservation authority.

Callers must prove complete source events under the eventual database lock.
These functions do not read or write balances, grant permissions, or post funds.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from itertools import pairwise

from medops.common.budget_planning import object_payload
from medops.common.domain_service import fingerprint_json
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount, signed_decimal_amount
from medops.common.organization import normalize_reference, normalize_required_date, validate_date_range

KEY_FIELDS = frozenset(
	{
		"company",
		"hospital",
		"organization_unit",
		"fiscal_year",
		"budget_subject",
		"funding_source",
		"project",
		"period_start",
		"period_end",
	}
)
BALANCE_FIELDS = frozenset(
	{"approved", "adjustment_in", "adjustment_out", "reserved", "committed", "actual", "released"}
)
MAX_CONTROL_KEYS = 1000
GUARD_SCOPE_PREFIX = "medops:budget:guard:v3:scope:"
GUARD_PERIOD_PREFIX = "medops:budget:guard:v3:period:"


@dataclass(frozen=True, slots=True)
class BudgetControlKey:
	company: str
	hospital: str
	organization_unit: str
	fiscal_year: str
	budget_subject: str
	funding_source: str
	project: str | None
	period_start: str
	period_end: str

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
		for name in ("period_start", "period_end"):
			value = getattr(self, name)
			if not isinstance(value, str) or normalize_required_date(value, label=name) != value:
				raise ValueError("budget key must be canonical")
		validate_date_range(self.period_start, self.period_end)

	@property
	def identity(self) -> str:
		return fingerprint_json(asdict(self))


def budget_control_key(value: object) -> BudgetControlKey:
	"""Normalize lexical dimensions; this does not resolve database identities."""
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
		period_start=normalize_required_date(data["period_start"], label="period_start"),
		period_end=normalize_required_date(data["period_end"], label="period_end"),
	)


def ordered_budget_keys(value: object) -> tuple[BudgetControlKey, ...]:
	"""Validate the entire lock set before writing, then impose one global order."""
	if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= MAX_CONTROL_KEYS:
		raise ValueError("bounded nonempty budget keys required")
	keys: dict[str, BudgetControlKey] = {}
	for key in value:
		if type(key) is not BudgetControlKey:
			raise ValueError("canonical budget keys required")
		identity = key.identity
		if identity in keys and keys[identity] != key:
			raise ValueError("budget key identity collision")
		keys[identity] = key
	return tuple(keys[identity] for identity in sorted(keys))


def budget_guard_names(value: object) -> tuple[str, ...]:
	"""Generate globally ordered scope and period locks for canonical keys.

	The common scope excludes year and dates so overlapping periods across
	versions and fiscal years cannot run concurrently when all writers comply.
	"""
	ordered = ordered_budget_keys(value)
	names: set[str] = set()
	for key in ordered:
		names.add(GUARD_SCOPE_PREFIX + fingerprint_json(_budget_scope(key)))
		names.add(GUARD_PERIOD_PREFIX + key.identity)
	return tuple(sorted(names))


def _budget_scope(key: BudgetControlKey) -> dict[str, str | None]:
	return {
		"company": key.company,
		"hospital": key.hospital,
		"organization_unit": key.organization_unit,
		"budget_subject": key.budget_subject,
		"funding_source": key.funding_source,
		"project": key.project,
	}


def require_nonoverlapping_budget_periods(value: object) -> None:
	"""Reject different overlapping keys in the same scope, inclusively.

	Callers must supply the complete relevant existing and proposed keys while
	holding their common scope guards; this pure check proves neither condition.
	Persisted names must already be resolved, as for budget_guard_names.
	Exact duplicate keys are one period. Different fiscal-year identities with
	the same dates are rejected as ambiguous rather than silently merged.
	"""
	groups: dict[tuple[str | None, ...], list[BudgetControlKey]] = {}
	for key in ordered_budget_keys(value):
		groups.setdefault(tuple(_budget_scope(key).values()), []).append(key)
	for keys in groups.values():
		ordered = sorted(keys, key=lambda key: (key.period_start, key.period_end, key.fiscal_year))
		for previous, current in pairwise(ordered):
			if current.period_start <= previous.period_end:
				raise ValueError("overlapping budget periods")


def available_budget(value: object, precision: int) -> str:
	"""Calculate BR-BUD-001 from a complete, independently verified aggregate.

	A negative result remains visible for reconciliation and cannot authorize a
	new reservation. Completeness, event linkage, currency, and locking are the
	responsibility of the future transactional service, not this reducer. Its
	approved component must come from executed budget release facts, not an
	approval that has not been released.
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
