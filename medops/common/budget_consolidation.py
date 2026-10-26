from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Any

from medops.common.budget_approval import (
	MAX_SUBMISSION_TOTAL,
	DepartmentSnapshot,
	SubmissionContext,
	SubmissionLine,
)
from medops.common.budget_planning import object_payload
from medops.common.budget_workflow import case_identity
from medops.common.domain_service import fingerprint_json, normalize_sha256
from medops.common.exact_money import exact_decimal_amount
from medops.common.organization import (
	normalize_optional_text,
	normalize_positive_integer,
	normalize_reference,
	normalize_required_date,
	normalize_required_text,
)

MAX_SOURCES = 1000
MAX_CONSOLIDATED_LINES = 50000
MAX_CONSOLIDATED_BYTES = 64 * 1024 * 1024
MAX_SOURCE_PROOF_BYTES = 2 * 1024 * 1024


def consolidation_digest(value: object) -> str:
	"""Stream already normalized v2 facts; leave all shared JSON boundaries intact."""
	encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
	digest = sha256()
	size = 0
	for chunk in encoder.iterencode(value):
		part = chunk.encode("utf-8")
		size += len(part)
		if size > MAX_CONSOLIDATED_BYTES:
			raise ValueError("consolidated facts exceed bounded storage")
		digest.update(part)
	return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class SourceSelection:
	submission: str
	expected_revision: int
	approval_case: str
	expected_approval_revision: int
	snapshot_digest: str


@dataclass(frozen=True, slots=True)
class ConsolidationCommand:
	organization_unit: str
	subject: str
	remarks: str | None
	sources: tuple[SourceSelection, ...]

	def request_payload(self):
		return dict(
			organization_unit=self.organization_unit,
			subject=self.subject,
			remarks=self.remarks,
			source_count=len(self.sources),
			selections_digest=consolidation_digest([asdict(row) for row in self.sources]),
		)


def build_consolidation(value: object) -> ConsolidationCommand:
	data = object_payload(value, {"organization_unit", "subject", "remarks", "sources"})
	rows = data.get("sources")
	if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_SOURCES:
		raise ValueError("invalid consolidation source count")
	sources = []
	for value in rows:
		row = object_payload(
			value,
			{
				"submission",
				"expected_revision",
				"approval_case",
				"expected_approval_revision",
				"snapshot_digest",
			},
		)
		sources.append(
			SourceSelection(
				submission=normalize_reference(row.get("submission"), label="submission"),
				expected_revision=normalize_positive_integer(
					row.get("expected_revision"), label="expected_revision"
				),
				approval_case=normalize_reference(row.get("approval_case"), label="approval_case"),
				expected_approval_revision=normalize_positive_integer(
					row.get("expected_approval_revision"), label="expected_approval_revision"
				),
				snapshot_digest=normalize_sha256(row.get("snapshot_digest"), label="snapshot_digest"),
			)
		)
	if len({row.submission for row in sources}) != len(sources):
		raise ValueError("duplicate department source")
	return ConsolidationCommand(
		organization_unit=normalize_reference(data.get("organization_unit"), label="organization_unit"),
		subject=normalize_required_text(data.get("subject"), label="subject", maximum=140),
		remarks=normalize_optional_text(data.get("remarks"), label="remarks", maximum=1000),
		sources=tuple(sorted(sources, key=lambda row: row.submission)),
	)


@dataclass(frozen=True, slots=True)
class ApprovedDepartment:
	submission: str
	snapshot_digest: str
	approval_case: str
	approval_round: int
	approval_revision: int
	approval_decision_digest: str
	approval_final_review: str
	approval_event_digest: str
	origin_plan: str
	preparer: str
	applicant: str
	requested_by: str

	def as_dict(self):
		return asdict(self)

	@classmethod
	def from_payload(cls, value: object):
		data = object_payload(value, set(cls.__dataclass_fields__))
		result = cls(
			submission=normalize_reference(data.get("submission"), label="submission"),
			snapshot_digest=normalize_sha256(data.get("snapshot_digest"), label="snapshot_digest"),
			approval_case=normalize_reference(data.get("approval_case"), label="approval_case"),
			approval_round=normalize_positive_integer(data.get("approval_round"), label="approval_round"),
			approval_revision=normalize_positive_integer(
				data.get("approval_revision"), label="approval_revision"
			),
			approval_decision_digest=normalize_sha256(
				data.get("approval_decision_digest"), label="approval_decision_digest"
			),
			approval_final_review=normalize_reference(
				data.get("approval_final_review"), label="approval_final_review"
			),
			approval_event_digest=normalize_sha256(
				data.get("approval_event_digest"), label="approval_event_digest"
			),
			origin_plan=normalize_reference(data.get("origin_plan"), label="origin_plan"),
			preparer=normalize_reference(data.get("preparer"), label="preparer"),
			applicant=normalize_reference(data.get("applicant"), label="applicant"),
			requested_by=normalize_reference(data.get("requested_by"), label="requested_by"),
		)
		if (
			result.approval_revision < 2
			or case_identity(result.submission, result.approval_round) != result.approval_case
		):
			raise ValueError("invalid approved source case identity")
		return result


def source_references(value: object) -> tuple[ApprovedDepartment, ...]:
	if isinstance(value, str):
		if len(value.encode("utf-8")) > MAX_SOURCE_PROOF_BYTES:
			raise ValueError("source proof exceeds bounds")
		value = json.loads(value)
	if not isinstance(value, list) or not 1 <= len(value) <= MAX_SOURCES:
		raise ValueError("invalid source proof")
	rows = tuple(
		sorted((ApprovedDepartment.from_payload(row) for row in value), key=lambda row: row.submission)
	)
	if len({row.submission for row in rows}) != len(rows) or len({row.origin_plan for row in rows}) != len(
		rows
	):
		raise ValueError("duplicate original department plan")
	return rows


def validate_grains(lines: tuple[SubmissionLine, ...]):
	if not 1 <= len(lines) <= MAX_CONSOLIDATED_LINES or len({line.fact_key for line in lines}) != len(lines):
		raise ValueError("duplicate or oversized original facts")
	periods: dict[tuple, list[tuple[str, str]]] = {}
	for line in lines:
		key = (
			line.organization_unit,
			line.cost_center,
			line.budget_subject,
			line.funding_source,
			line.account,
			line.project,
		)
		periods.setdefault(key, []).append((line.period_start, line.period_end))
	for rows in periods.values():
		previous_end = None
		for start, end in sorted(rows):
			if previous_end is not None and start <= previous_end:
				raise ValueError("overlapping original financial periods")
			previous_end = end


@dataclass(frozen=True, slots=True)
class ConsolidatedSnapshot:
	context: SubmissionContext
	organization_unit: str
	subject: str
	applicant: str
	posting_date: str
	remarks: str | None
	sources: tuple[ApprovedDepartment, ...]
	lines: tuple[SubmissionLine, ...]

	@property
	def cost_center(self):
		# The original cost centers remain on every leaf; no synthetic posting center.
		return None

	@property
	def source_set_digest(self):
		return consolidation_digest([row.as_dict() for row in self.sources])

	@property
	def identity_key(self):
		return fingerprint_json(
			dict(
				kind="Consolidated",
				organization_unit=self.organization_unit,
				source_set_digest=self.source_set_digest,
			)
		)

	@property
	def total_amount(self):
		return exact_decimal_amount(
			sum((Decimal(row.amount) for row in self.lines), Decimal(0)),
			self.context.currency_precision,
			maximum=MAX_SUBMISSION_TOTAL,
		)

	def facts(self) -> dict[str, Any]:
		return dict(
			schema_version=2,
			submission_type="Consolidated",
			**asdict(self.context),
			organization_unit=self.organization_unit,
			cost_center=None,
			subject=self.subject,
			applicant=self.applicant,
			posting_date=self.posting_date,
			remarks=self.remarks,
			source_set_digest=self.source_set_digest,
			sources=[row.as_dict() for row in self.sources],
			total_amount=self.total_amount,
			lines=[row.as_dict() for row in self.lines],
		)

	@property
	def snapshot_digest(self):
		return consolidation_digest(self.facts())

	def as_dict(self) -> dict[str, Any]:
		return dict(identity_key=self.identity_key, snapshot_digest=self.snapshot_digest, **self.facts())


def consolidated_snapshot_from_payload(data: dict[str, Any]) -> ConsolidatedSnapshot:
	if (
		type(data.get("schema_version")) is not int
		or data["schema_version"] != 2
		or data.get("submission_type") != "Consolidated"
	):
		raise ValueError("unsupported consolidation contract")
	context = SubmissionContext.from_payload(data)
	sources = source_references(data.get("sources"))
	items = data.get("lines")
	if (
		not isinstance(items, list)
		or not 1 <= len(items) <= MAX_CONSOLIDATED_LINES
		or any(not isinstance(row, dict) for row in items)
	):
		raise ValueError("invalid consolidated lines")
	lines = tuple(
		sorted(
			(SubmissionLine.from_payload(row, context.currency_precision) for row in items),
			key=lambda row: row.fact_key,
		)
	)
	validate_grains(lines)
	if {row.origin_plan for row in lines} != {row.origin_plan for row in sources}:
		raise ValueError("missing or foreign original source plan")
	if data.get("cost_center") is not None:
		raise ValueError("consolidated header cannot replace original cost centers")
	snapshot = ConsolidatedSnapshot(
		context=context,
		**{key: normalize_reference(data.get(key), label=key) for key in ("organization_unit", "applicant")},
		subject=normalize_required_text(data.get("subject"), label="subject", maximum=140),
		posting_date=normalize_required_date(data.get("posting_date"), label="posting_date"),
		remarks=normalize_optional_text(data.get("remarks"), label="remarks", maximum=1000),
		sources=sources,
		lines=lines,
	)
	for key in ("source_set_digest", "total_amount", "identity_key", "snapshot_digest"):
		if key in data and data[key] != getattr(snapshot, key):
			raise ValueError("consolidation proof or exact total differs")
	return snapshot


def consolidate_departments(
	departments: tuple[tuple[ApprovedDepartment, DepartmentSnapshot], ...],
	*,
	organization_unit: str,
	subject: str,
	applicant: str,
	posting_date: str,
	remarks=None,
) -> ConsolidatedSnapshot:
	if not 1 <= len(departments) <= MAX_SOURCES:
		raise ValueError("invalid department source count")
	if not isinstance(departments[0][1], DepartmentSnapshot):
		raise ValueError("only department leaves can be consolidated")
	context = departments[0][1].context
	for ref, snapshot in departments:
		if not isinstance(snapshot, DepartmentSnapshot) or snapshot.context != context:
			raise ValueError("only compatible department leaves can be consolidated")
		if (
			ref.submission != snapshot.identity_key
			or ref.snapshot_digest != snapshot.snapshot_digest
			or ref.origin_plan != snapshot.source_plan
			or ref.applicant != snapshot.applicant
		):
			raise ValueError("approved source differs from frozen facts")
	return consolidated_snapshot_from_payload(
		dict(
			schema_version=2,
			submission_type="Consolidated",
			**asdict(context),
			organization_unit=organization_unit,
			subject=subject,
			applicant=applicant,
			posting_date=posting_date,
			remarks=remarks,
			sources=[ref.as_dict() for ref, _ in departments],
			lines=[row.as_dict() for _, snapshot in departments for row in snapshot.lines],
		)
	)
