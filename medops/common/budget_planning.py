from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal

from medops.common.domain_service import fingerprint_json
from medops.common.exact_money import ExactMoneyError, exact_decimal_amount
from medops.common.organization import (
	normalize_boolean,
	normalize_code,
	normalize_nonnegative_integer,
	normalize_optional_date,
	normalize_optional_text,
	normalize_positive_integer,
	normalize_reference,
	normalize_required_date,
	normalize_required_text,
	validate_date_range,
)

VERSION = "HRP Budget Version"
PLAN = "HRP Budget Plan"
LINE = "HRP Budget Plan Line"
DIMENSION = "HRP Business Dimension"
VALUE = "HRP Dimension Value"
FUND = "HRP Funding Source"
MASTER_TYPES = {"dimension": DIMENSION, "value": VALUE, "fund": FUND}
FREQUENCIES = ("Monthly", "Quarterly", "Half-Yearly", "Yearly")
PURPOSES = ("Budget Subject", "Analytical")
FUND_TYPES = ("Fiscal Appropriation", "Own Revenue", "Grant", "Other")
MAX_LINES = 5000
MAX_PLANS = 1000
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_AMOUNT = Decimal("999999999999.999999999")
# The exact decimal wire/storage fields are reviewed numeric sources, even though
# Frappe's Currency persistence converts to binary float. No arbitrary Data field
# becomes a numeric business-rule source.
DECIMAL_DATA_FIELDS = frozenset({(PLAN, "total_amount"), (LINE, "amount")})
_master_write: ContextVar[bool] = ContextVar("medops_budget_master_write", default=False)
_planning_write: ContextVar[str | None] = ContextVar("medops_budget_planning_write", default=None)


class BudgetPlanningError(ValueError):
	"""A bounded planning command violates the budget contract."""


@contextmanager
def master_write():
	token = _master_write.set(True)
	try:
		yield
	finally:
		_master_write.reset(token)


def master_write_active() -> bool:
	return _master_write.get()


@contextmanager
def planning_write(operation: str):
	if operation not in {"save_version", "freeze", "clone", "save_plan", "withdraw"}:
		raise BudgetPlanningError("unknown planning operation")
	token = _planning_write.set(operation)
	try:
		yield
	finally:
		_planning_write.reset(token)


def planning_operation() -> str | None:
	return _planning_write.get()


def object_payload(value: object, fields: set[str]) -> dict[str, Any]:
	if isinstance(value, str):
		if len(value.encode("utf-8")) > MAX_PAYLOAD_BYTES:
			raise BudgetPlanningError("planning payload exceeds bounds")
		try:
			value = json.loads(value)
		except (ValueError, RecursionError) as exc:
			raise BudgetPlanningError("invalid planning JSON") from exc
	if not isinstance(value, dict) or set(value) - fields:
		raise BudgetPlanningError("unknown planning fields")
	return value


def choice(value: object, choices: tuple[str, ...], *, label: str) -> str:
	if not isinstance(value, str) or value not in choices:
		raise BudgetPlanningError(f"invalid {label}")
	return value


def decimal_amount(value: object, precision: int | None = None) -> str:
	try:
		return exact_decimal_amount(value, precision, maximum=MAX_AMOUNT)
	except ExactMoneyError as exc:
		raise BudgetPlanningError(str(exc)) from exc


def amount_total(values: list[str], precision: int) -> str:
	return decimal_amount(sum((Decimal(decimal_amount(v, precision)) for v in values), Decimal(0)), precision)


def allocate_amount(value: object, periods: int, precision: int) -> list[str]:
	if isinstance(periods, bool) or not isinstance(periods, int) or not 1 <= periods <= 366:
		raise BudgetPlanningError("invalid period count")
	amount = Decimal(decimal_amount(value, precision))
	quantum = Decimal(1).scaleb(-precision)
	units = int(amount / quantum)
	base, remainder = divmod(units, periods)
	return [format(Decimal(base + int(i < remainder)) * quantum, "f") for i in range(periods)]


def planning_digest(value: object) -> str:
	"""Hash only already-normalized typed budget data without weakening the shared JSON boundary."""
	try:
		payload = json.dumps(
			value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
		)
	except (ValueError, TypeError, RecursionError) as exc:
		raise BudgetPlanningError("invalid typed planning data") from exc
	if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
		raise BudgetPlanningError("typed planning data exceeds bounds")
	return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MasterUpsert:
	kind: str
	company: str
	hospital: str
	code: str
	display_name: str
	enabled: bool
	valid_from: str
	valid_to: str | None
	expected_revision: int
	remarks: str | None
	dimension: str | None = None
	purpose: str | None = None
	source_type: str | None = None

	def as_request_payload(self) -> dict[str, Any]:
		return asdict(self)


def build_master(kind: object, value: object) -> MasterUpsert:
	if not isinstance(kind, str) or kind not in MASTER_TYPES:
		raise BudgetPlanningError("unknown budget master kind")
	fields = {
		"company",
		"hospital",
		"code",
		"display_name",
		"enabled",
		"valid_from",
		"valid_to",
		"expected_revision",
		"remarks",
	}
	fields |= {"dimension"} if kind == "value" else {"purpose"} if kind == "dimension" else {"source_type"}
	data = object_payload(value, fields)
	start = normalize_required_date(data.get("valid_from"), label="valid_from")
	end = normalize_optional_date(data.get("valid_to"), label="valid_to")
	validate_date_range(start, end)
	return MasterUpsert(
		kind=kind,
		company=normalize_reference(data.get("company"), label="company"),
		hospital=normalize_reference(data.get("hospital"), label="hospital"),
		code=normalize_code(data.get("code"), label="code"),
		display_name=normalize_required_text(data.get("display_name"), label="display_name", maximum=140),
		enabled=normalize_boolean(data.get("enabled", True), label="enabled"),
		valid_from=start,
		valid_to=end,
		expected_revision=normalize_nonnegative_integer(
			data.get("expected_revision", 0), label="expected_revision"
		),
		remarks=normalize_optional_text(data.get("remarks"), label="remarks", maximum=1000),
		dimension=normalize_reference(data.get("dimension"), label="dimension") if kind == "value" else None,
		purpose=choice(data.get("purpose"), PURPOSES, label="purpose") if kind == "dimension" else None,
		source_type=choice(data.get("source_type"), FUND_TYPES, label="source_type")
		if kind == "fund"
		else None,
	)


def master_identity(command: MasterUpsert) -> str:
	return fingerprint_json(
		{key: getattr(command, key) for key in ("kind", "company", "hospital", "dimension", "code")}
	)


@dataclass(frozen=True, slots=True)
class VersionSave:
	name: str | None
	company: str
	hospital: str
	organization_version: str
	fiscal_year: str
	subject: str
	distribution_frequency: str
	remarks: str | None
	expected_revision: int

	def as_request_payload(self) -> dict[str, Any]:
		return asdict(self)


def build_version(value: object) -> VersionSave:
	data = object_payload(
		value,
		{
			"name",
			"company",
			"hospital",
			"organization_version",
			"fiscal_year",
			"subject",
			"distribution_frequency",
			"remarks",
			"expected_revision",
		},
	)
	name = normalize_optional_text(data.get("name"), label="name", maximum=140)
	revision = normalize_nonnegative_integer(data.get("expected_revision", 0), label="expected_revision")
	if bool(name) != bool(revision):
		raise BudgetPlanningError("existing version requires its revision")
	return VersionSave(
		name=name,
		**{
			key: normalize_reference(data.get(key), label=key)
			for key in ("company", "hospital", "organization_version", "fiscal_year")
		},
		subject=normalize_required_text(data.get("subject"), label="subject", maximum=140),
		distribution_frequency=choice(
			data.get("distribution_frequency", "Monthly"), FREQUENCIES, label="distribution_frequency"
		),
		remarks=normalize_optional_text(data.get("remarks"), label="remarks", maximum=1000),
		expected_revision=revision,
	)


@dataclass(frozen=True, slots=True)
class PlanLine:
	budget_subject: str
	funding_source: str
	account: str
	project: str | None
	period_index: int
	amount: str
	description: str | None

	def as_request_payload(self) -> dict[str, Any]:
		return asdict(self)


def line_identity(line: PlanLine | dict[str, Any]) -> str:
	values = line.as_request_payload() if isinstance(line, PlanLine) else line
	return fingerprint_json(
		{
			key: values.get(key)
			for key in ("budget_subject", "funding_source", "account", "project", "period_index")
		}
	)


def line_sort_key(line: PlanLine) -> tuple[str, str, str, str, int]:
	return (line.budget_subject, line.funding_source, line.account, line.project or "", line.period_index)


def version_definition(payload: dict[str, Any]) -> dict[str, Any]:
	return {
		key: payload[key]
		for key in (
			"name",
			"company",
			"hospital",
			"organization_version",
			"fiscal_year",
			"version_number",
			"subject",
			"currency",
			"currency_precision",
			"period_start",
			"period_end",
			"distribution_frequency",
			"cloned_from",
			"remarks",
		)
	}


def plan_definition(payload: dict[str, Any]) -> dict[str, Any]:
	return {
		key: payload[key]
		for key in (
			"name",
			"company",
			"hospital",
			"budget_version",
			"organization_version",
			"organization_unit",
			"cost_center",
			"fiscal_year",
			"currency",
			"currency_precision",
			"subject",
			"remarks",
			"total_amount",
			"lines",
		)
	}


def build_lines(value: object) -> tuple[PlanLine, ...]:
	if not isinstance(value, list) or not 1 <= len(value) <= MAX_LINES:
		raise BudgetPlanningError("one to five thousand planning lines required")
	rows = []
	seen = set()
	for item in value:
		data = object_payload(
			item,
			{
				"budget_subject",
				"funding_source",
				"account",
				"project",
				"period_index",
				"amount",
				"description",
			},
		)
		period = normalize_positive_integer(data.get("period_index"), label="period_index")
		if period > 366:
			raise BudgetPlanningError("period index exceeds bounds")
		row = PlanLine(
			**{
				key: normalize_reference(data.get(key), label=key)
				for key in ("budget_subject", "funding_source", "account")
			},
			project=normalize_optional_text(data.get("project"), label="project", maximum=140),
			period_index=period,
			amount=decimal_amount(data.get("amount")),
			description=normalize_optional_text(data.get("description"), label="description", maximum=500),
		)
		identity = line_identity(row)
		if identity in seen:
			raise BudgetPlanningError("duplicate budget grain and period")
		seen.add(identity)
		rows.append(row)
	return tuple(sorted(rows, key=line_sort_key))


@dataclass(frozen=True, slots=True)
class PlanSave:
	name: str | None
	budget_version: str
	organization_unit: str
	subject: str
	remarks: str | None
	expected_revision: int
	lines: tuple[PlanLine, ...]

	def as_request_payload(self) -> dict[str, Any]:
		# Keep the shared encrypted idempotency snapshot bounded for large plans.
		return {
			"name": self.name,
			"budget_version": self.budget_version,
			"organization_unit": self.organization_unit,
			"subject": self.subject,
			"remarks": self.remarks,
			"expected_revision": self.expected_revision,
			"lines_digest": planning_digest([line.as_request_payload() for line in self.lines]),
			"line_count": len(self.lines),
		}


def build_plan(value: object) -> PlanSave:
	data = object_payload(
		value,
		{"name", "budget_version", "organization_unit", "subject", "remarks", "expected_revision", "lines"},
	)
	name = normalize_optional_text(data.get("name"), label="name", maximum=140)
	revision = normalize_nonnegative_integer(data.get("expected_revision", 0), label="expected_revision")
	if bool(name) != bool(revision):
		raise BudgetPlanningError("existing plan requires its revision")
	return PlanSave(
		name=name,
		budget_version=normalize_reference(data.get("budget_version"), label="budget_version"),
		organization_unit=normalize_reference(data.get("organization_unit"), label="organization_unit"),
		subject=normalize_required_text(data.get("subject"), label="subject", maximum=140),
		remarks=normalize_optional_text(data.get("remarks"), label="remarks", maximum=1000),
		expected_revision=revision,
		lines=build_lines(data.get("lines")),
	)


@dataclass(frozen=True, slots=True)
class PlanningAction:
	name: str
	expected_revision: int
	subject: str | None = None

	def as_request_payload(self) -> dict[str, Any]:
		return asdict(self)


def build_action(value: object, *, allow_subject: bool = False) -> PlanningAction:
	data = object_payload(value, {"name", "expected_revision"} | ({"subject"} if allow_subject else set()))
	return PlanningAction(
		name=normalize_reference(data.get("name"), label="name"),
		expected_revision=normalize_positive_integer(
			data.get("expected_revision"), label="expected_revision"
		),
		subject=normalize_optional_text(data.get("subject"), label="subject", maximum=140),
	)


PlanningKind = Literal["version", "plan"]
