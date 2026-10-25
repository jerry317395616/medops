from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from medops.common.budget_planning import (
	MAX_LINES,
	decimal_amount,
	line_identity,
	plan_definition,
	planning_digest,
)
from medops.common.domain_service import fingerprint_json, normalize_sha256
from medops.common.exact_money import exact_decimal_amount
from medops.common.organization import (
	normalize_nonnegative_integer,
	normalize_optional_text,
	normalize_positive_integer,
	normalize_reference,
	normalize_required_date,
	validate_date_range,
)

SUBMISSION = "HRP Budget Submission"
SUBMISSION_LINE = "HRP Budget Submission Line"
MAX_SUBMISSION_TOTAL = Decimal("999999999999999.999999999")
_sealing: ContextVar[bool] = ContextVar("medops_budget_submission_sealing", default=False)


@contextmanager
def submission_sealing():
	token = _sealing.set(True)
	try:
		yield
	finally:
		_sealing.reset(token)


def submission_sealing_active() -> bool:
	return _sealing.get()


@dataclass(frozen=True, slots=True)
class DepartmentSubmission:
	source_plan: str
	expected_revision: int


def build_department_submission(value: object) -> DepartmentSubmission:
	from medops.common.budget_planning import object_payload

	data = object_payload(value, {"source_plan", "expected_revision"})
	return DepartmentSubmission(
		source_plan=normalize_reference(data.get("source_plan"), label="source_plan"),
		expected_revision=normalize_positive_integer(
			data.get("expected_revision"), label="expected_revision"
		),
	)


@dataclass(frozen=True, slots=True)
class SubmissionContext:
	company: str
	hospital: str
	organization_version: str
	fiscal_year: str
	currency: str
	currency_precision: int

	@classmethod
	def from_payload(cls, data: dict[str, Any]) -> SubmissionContext:
		precision = normalize_nonnegative_integer(data.get("currency_precision"), label="currency_precision")
		if precision > 9:
			raise ValueError("invalid captured precision")
		return cls(
			**{
				key: normalize_reference(data.get(key), label=key)
				for key in ("company", "hospital", "organization_version", "fiscal_year", "currency")
			},
			currency_precision=precision,
		)


@dataclass(frozen=True, slots=True)
class SubmissionLine:
	origin_plan: str
	origin_line_key: str
	origin_version: str
	origin_version_digest: str
	origin_plan_digest: str
	organization_unit: str
	cost_center: str
	budget_subject: str
	funding_source: str
	account: str
	project: str | None
	period_index: int
	period_start: str
	period_end: str
	amount: str
	description: str | None

	@property
	def fact_key(self) -> str:
		return fingerprint_json({"plan": self.origin_plan, "line": self.origin_line_key})

	def as_dict(self) -> dict[str, Any]:
		return {"fact_key": self.fact_key, **asdict(self)}

	@classmethod
	def from_payload(cls, data: dict[str, Any], precision: int) -> SubmissionLine:
		start = normalize_required_date(data.get("period_start"), label="period_start")
		end = normalize_required_date(data.get("period_end"), label="period_end")
		validate_date_range(start, end)
		period = normalize_positive_integer(data.get("period_index"), label="period_index")
		if period > 366:
			raise ValueError("period index exceeds bounds")
		line = cls(
			**{
				key: normalize_reference(data.get(key), label=key)
				for key in (
					"origin_plan",
					"origin_version",
					"organization_unit",
					"cost_center",
					"budget_subject",
					"funding_source",
					"account",
				)
			},
			**{
				key: normalize_sha256(data.get(key), label=key)
				for key in ("origin_line_key", "origin_version_digest", "origin_plan_digest")
			},
			project=normalize_optional_text(data.get("project"), label="project", maximum=140),
			period_index=period,
			period_start=start,
			period_end=end,
			amount=decimal_amount(data.get("amount"), precision),
			description=normalize_optional_text(data.get("description"), label="description", maximum=500),
		)
		if line_identity(asdict(line)) != line.origin_line_key:
			raise ValueError("original budget grain does not match its key")
		if data.get("fact_key", line.fact_key) != line.fact_key:
			raise ValueError("invalid original fact identity")
		return line


@dataclass(frozen=True, slots=True)
class DepartmentSnapshot:
	context: SubmissionContext
	source_plan: str
	source_plan_revision: int
	source_plan_digest: str
	source_version: str
	source_version_digest: str
	organization_unit: str
	cost_center: str
	subject: str
	applicant: str
	posting_date: str
	remarks: str | None
	lines: tuple[SubmissionLine, ...]

	@property
	def identity_key(self) -> str:
		return fingerprint_json({"kind": "Department", "source_plan": self.source_plan})

	@property
	def total_amount(self) -> str:
		return exact_decimal_amount(
			sum((Decimal(line.amount) for line in self.lines), Decimal(0)),
			self.context.currency_precision,
			maximum=MAX_SUBMISSION_TOTAL,
		)

	def facts(self) -> dict[str, Any]:
		return {
			"schema_version": 1,
			"submission_type": "Department",
			**asdict(self.context),
			**{
				key: getattr(self, key)
				for key in (
					"source_plan",
					"source_plan_revision",
					"source_plan_digest",
					"source_version",
					"source_version_digest",
					"organization_unit",
					"cost_center",
					"subject",
					"applicant",
					"posting_date",
					"remarks",
				)
			},
			"total_amount": self.total_amount,
			"lines": [line.as_dict() for line in self.lines],
		}

	@property
	def snapshot_digest(self) -> str:
		return planning_digest(self.facts())

	def as_dict(self) -> dict[str, Any]:
		return {"identity_key": self.identity_key, "snapshot_digest": self.snapshot_digest, **self.facts()}


def department_snapshot_from_payload(data: dict[str, Any]) -> DepartmentSnapshot:
	if (
		type(data.get("schema_version")) is not int
		or data["schema_version"] != 1
		or data.get("submission_type") != "Department"
	):
		raise ValueError("unsupported submission snapshot contract")
	context = SubmissionContext.from_payload(data)
	items = data.get("lines")
	if not isinstance(items, list) or not 1 <= len(items) <= MAX_LINES:
		raise ValueError("invalid submission line count")
	if any(not isinstance(item, dict) for item in items):
		raise ValueError("invalid submission line")
	lines = tuple(
		sorted(
			(SubmissionLine.from_payload(item, context.currency_precision) for item in items),
			key=lambda line: line.fact_key,
		)
	)
	if len({line.fact_key for line in lines}) != len(lines):
		raise ValueError("duplicate original budget fact")
	snapshot = DepartmentSnapshot(
		context=context,
		**{
			key: normalize_reference(data.get(key), label=key)
			for key in (
				"source_plan",
				"source_version",
				"organization_unit",
				"cost_center",
				"subject",
				"applicant",
			)
		},
		**{
			key: normalize_sha256(data.get(key), label=key)
			for key in ("source_plan_digest", "source_version_digest")
		},
		source_plan_revision=normalize_positive_integer(
			data.get("source_plan_revision"), label="source_plan_revision"
		),
		posting_date=normalize_required_date(data.get("posting_date"), label="posting_date"),
		remarks=normalize_optional_text(data.get("remarks"), label="remarks", maximum=1000),
		lines=lines,
	)
	for line in lines:
		if any(
			getattr(line, line_key) != getattr(snapshot, parent_key)
			for line_key, parent_key in (
				("origin_plan", "source_plan"),
				("origin_version", "source_version"),
				("origin_version_digest", "source_version_digest"),
				("origin_plan_digest", "source_plan_digest"),
				("organization_unit", "organization_unit"),
				("cost_center", "cost_center"),
			)
		):
			raise ValueError("department submission contains foreign facts")
	if "total_amount" in data and data["total_amount"] != snapshot.total_amount:
		raise ValueError("submission total does not match exact facts")
	for key in ("identity_key", "snapshot_digest"):
		if key in data and data[key] != getattr(snapshot, key):
			raise ValueError("submission identity or digest does not match")
	return snapshot


def freeze_department_facts(
	plan: dict[str, Any],
	version: dict[str, Any],
	*,
	version_digest: str,
	applicant: str,
	posting_date: str,
) -> DepartmentSnapshot:
	if version.get("docstatus") != 1 or version.get("status") != "Frozen":
		raise ValueError("a frozen budget version is required")
	if plan.get("docstatus") != 0 or plan.get("status") != "Draft":
		raise ValueError("only an included plan can be submitted")
	context = SubmissionContext.from_payload(plan)
	if context != SubmissionContext.from_payload(version) or plan.get("budget_version") != version.get(
		"name"
	):
		raise ValueError("plan and frozen version context differ")
	version_digest = normalize_sha256(version_digest, label="version_digest")
	plan_digest = planning_digest(plan_definition(plan))
	items = plan.get("lines")
	if not isinstance(items, list):
		raise ValueError("invalid frozen plan lines")
	lines = []
	for row in items:
		if not isinstance(row, dict):
			raise ValueError("invalid frozen plan line")
		if (
			not str(version.get("period_start", ""))
			<= str(row.get("period_start", ""))
			<= str(row.get("period_end", ""))
			<= str(version.get("period_end", ""))
		):
			raise ValueError("budget fact falls outside the fiscal calendar")
		lines.append(
			{
				**row,
				"origin_plan": plan.get("name"),
				"origin_line_key": line_identity(row),
				"origin_version": version.get("name"),
				"origin_version_digest": version_digest,
				"origin_plan_digest": plan_digest,
				"organization_unit": plan.get("organization_unit"),
				"cost_center": plan.get("cost_center"),
			}
		)
	return department_snapshot_from_payload(
		{
			"schema_version": 1,
			"submission_type": "Department",
			**asdict(context),
			"source_plan": plan.get("name"),
			"source_plan_revision": plan.get("revision"),
			"source_plan_digest": plan_digest,
			"source_version": version.get("name"),
			"source_version_digest": version_digest,
			**{
				key: plan.get(key)
				for key in ("organization_unit", "cost_center", "subject", "remarks", "total_amount")
			},
			"applicant": applicant,
			"posting_date": posting_date,
			"lines": lines,
		}
	)
