from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, TypedDict

from medops.common.domain_service import canonical_json, fingerprint_json
from medops.common.exact_money import (
	MAX_APPROVAL_AMOUNT,
	ExactMoneyError,
	canonical_decimal,
	exact_decimal_amount,
)
from medops.common.organization import (
	OrganizationContractError,
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

APPROVAL_MATRIX_SCHEMA_VERSION = 1
EXACT_APPROVAL_SCHEMA_VERSION = 2
EXACT_APPROVAL_TARGETS = frozenset({"HRP Budget Approval"})
APPROVAL_MATRIX_DOCTYPE = "HRP Approval Matrix"
APPROVAL_MATRIX_ROW_DOCTYPE = "HRP Approval Matrix Row"
APPROVER_TYPES = ("Role", "User")
APPROVAL_MODES = ("All", "Any")
MAX_APPROVAL_STEPS = 100
MAX_DIMENSIONS = 16
MAX_DIMENSIONS_BYTES = 16 * 1024
MAX_AMOUNT = Decimal("999999999999999.99")
DIMENSION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CONTEXT_DIMENSIONS = frozenset({"company", "hospital", "organization_unit", "effective_on"})
ApproverType = Literal["Role", "User"]
ApprovalMode = Literal["All", "Any"]


class ApprovalMatrixContractError(ValueError):
	"""Raised when an approval matrix contains unsafe or ambiguous policy data."""


class DelegatedApproverEvidence(TypedDict, total=False):
	delegated_from: str
	delegation: str


class ResolvedApprover(DelegatedApproverEvidence):
	user: str
	source_type: ApproverType
	source_value: str


class ApprovalStepSources(TypedDict, total=False):
	source_assignments: list[ResolvedApprover]


class ResolvedApprovalStep(ApprovalStepSources):
	sequence_no: int
	step_name: str
	approval_mode: ApprovalMode
	threshold_amount: float | str
	approvers: list[ResolvedApprover]


class ExactApprovalDecisionContext(TypedDict, total=False):
	amount: str
	amount_precision: int
	context_digest: str
	policy_schema_version: int


class ApprovalDecision(ExactApprovalDecisionContext):
	schema_version: int
	doctype: str
	docname: str
	matrix: str
	matrix_code: str
	matrix_name: str
	policy_version: int
	policy_digest: str
	approvers: list[str]
	delegations: list[str]
	steps: list[ResolvedApprovalStep]
	decision_digest: str


def approval_amount_profile(schema_version: object = 1, amount_precision: object = 2) -> tuple[int, int]:
	try:
		version = normalize_positive_integer(schema_version, label="schema_version")
		precision = normalize_nonnegative_integer(amount_precision, label="amount_precision")
	except OrganizationContractError as exc:
		raise ApprovalMatrixContractError(str(exc)) from exc
	if version not in (1, 2) or precision > 9 or (version == 1 and precision != 2):
		raise ApprovalMatrixContractError("unsupported approval amount profile")
	return version, precision


def _decimal(value: object, *, label: str, schema_version: int = 1, amount_precision: int = 2) -> Decimal:
	if schema_version == EXACT_APPROVAL_SCHEMA_VERSION:
		try:
			return Decimal(exact_decimal_amount(value, amount_precision, maximum=MAX_APPROVAL_AMOUNT))
		except ExactMoneyError as exc:
			raise ApprovalMatrixContractError(f"{label}: {exc}") from exc
	# Preserve version1 parsing/quantization, including historical float inputs.
	if isinstance(value, bool):
		raise ApprovalMatrixContractError(f"{label} must be an amount")
	try:
		normalized = Decimal(str(value))
	except (InvalidOperation, ValueError) as exc:
		raise ApprovalMatrixContractError(f"{label} must be an amount") from exc
	if not normalized.is_finite() or normalized < 0 or normalized > MAX_AMOUNT:
		raise ApprovalMatrixContractError(f"{label} is outside the allowed range")
	exponent = normalized.as_tuple().exponent
	if not isinstance(exponent, int) or exponent < -2:
		raise ApprovalMatrixContractError(f"{label} must have at most two decimal places")
	return normalized.quantize(Decimal("0.01"))


def _load_dimensions(value: object, *, context: bool) -> dict[str, str]:
	loaded = value
	if value in (None, ""):
		loaded = {}
	elif isinstance(value, str):
		if len(value.encode("utf-8")) > MAX_DIMENSIONS_BYTES:
			raise ApprovalMatrixContractError("dimensions payload is too large")
		try:
			loaded = json.loads(value)
		except json.JSONDecodeError as exc:
			raise ApprovalMatrixContractError("dimensions must contain valid JSON") from exc
	if not isinstance(loaded, dict) or len(loaded) > MAX_DIMENSIONS:
		raise ApprovalMatrixContractError("dimensions must be a bounded object")
	try:
		if len(canonical_json(loaded).encode("utf-8")) > MAX_DIMENSIONS_BYTES:
			raise ApprovalMatrixContractError("dimensions payload is too large")
	except ValueError as exc:
		raise ApprovalMatrixContractError("dimensions must contain JSON-compatible values") from exc
	normalized: dict[str, str] = {}
	for key, raw in loaded.items():
		if not isinstance(key, str) or DIMENSION_KEY_PATTERN.fullmatch(key) is None:
			raise ApprovalMatrixContractError("dimension key is invalid")
		if not context and key in CONTEXT_DIMENSIONS:
			raise ApprovalMatrixContractError("organization context cannot be duplicated in dimensions")
		if not isinstance(raw, str):
			raise ApprovalMatrixContractError("dimension values must be text")
		value_text = normalize_required_text(raw, label=f"dimension {key}")
		normalized[key] = value_text
	if context:
		missing = CONTEXT_DIMENSIONS - set(normalized)
		if missing:
			raise ApprovalMatrixContractError("dimensions are missing organization context")
	return dict(sorted(normalized.items()))


@dataclass(frozen=True, slots=True)
class ApprovalMatrixStep:
	sequence_no: int
	step_name: str
	threshold_amount: Decimal
	approver_type: ApproverType
	approver_role: str | None
	approver_user: str | None
	approval_mode: ApprovalMode
	description: str | None
	schema_version: int = 1
	amount_precision: int = 2

	@property
	def source_value(self) -> str:
		value = self.approver_role if self.approver_type == "Role" else self.approver_user
		if value is None:
			raise ApprovalMatrixContractError("approval step source is missing")
		return value

	def as_dict(self) -> dict[str, object]:
		return {
			"sequence_no": self.sequence_no,
			"step_name": self.step_name,
			"threshold_amount": (
				float(self.threshold_amount)
				if self.schema_version == 1
				else canonical_decimal(self.threshold_amount)
			),
			"approver_type": self.approver_type,
			"approver_role": self.approver_role,
			"approver_user": self.approver_user,
			"approval_mode": self.approval_mode,
			"description": self.description,
		}


def build_approval_matrix_step(
	*,
	sequence_no: object,
	step_name: object,
	threshold_amount: object,
	approver_type: object,
	approver_role: object = None,
	approver_user: object = None,
	approval_mode: object = "All",
	description: object = None,
	schema_version: object = 1,
	amount_precision: object = 2,
) -> ApprovalMatrixStep:
	try:
		version, precision = approval_amount_profile(schema_version, amount_precision)
		sequence = normalize_positive_integer(sequence_no, label="sequence_no")
		name = normalize_required_text(step_name, label="step_name")
		if not isinstance(approver_type, str) or approver_type not in APPROVER_TYPES:
			raise ApprovalMatrixContractError("approver_type is invalid")
		if not isinstance(approval_mode, str) or approval_mode not in APPROVAL_MODES:
			raise ApprovalMatrixContractError("approval_mode is invalid")
		role = normalize_reference(approver_role, label="approver_role") if approver_role else None
		user = normalize_reference(approver_user, label="approver_user") if approver_user else None
		if approver_type == "Role" and (role is None or user is not None):
			raise ApprovalMatrixContractError("a role approver requires only approver_role")
		if approver_type == "User" and (user is None or role is not None):
			raise ApprovalMatrixContractError("a user approver requires only approver_user")
		return ApprovalMatrixStep(
			sequence_no=sequence,
			step_name=name,
			threshold_amount=_decimal(
				threshold_amount, label="threshold_amount", schema_version=version, amount_precision=precision
			),
			approver_type=approver_type,
			approver_role=role,
			approver_user=user,
			approval_mode=approval_mode,
			description=normalize_optional_text(description, label="description"),
			schema_version=version,
			amount_precision=precision,
		)
	except OrganizationContractError as exc:
		raise ApprovalMatrixContractError(str(exc)) from exc


def build_approval_matrix_steps(
	value: object, *, schema_version: object = 1, amount_precision: object = 2
) -> tuple[ApprovalMatrixStep, ...]:
	version, precision = approval_amount_profile(schema_version, amount_precision)
	loaded = value
	if isinstance(value, str):
		if len(value.encode("utf-8")) > MAX_DIMENSIONS_BYTES * 4:
			raise ApprovalMatrixContractError("steps payload is too large")
		try:
			loaded = json.loads(value)
		except json.JSONDecodeError as exc:
			raise ApprovalMatrixContractError("steps must contain valid JSON") from exc
	if not isinstance(loaded, list):
		raise ApprovalMatrixContractError("steps must be a list")
	if not 1 <= len(loaded) <= MAX_APPROVAL_STEPS:
		raise ApprovalMatrixContractError("approval step count is outside the allowed range")
	allowed = {
		"sequence_no",
		"step_name",
		"threshold_amount",
		"approver_type",
		"approver_role",
		"approver_user",
		"approval_mode",
		"description",
	}
	steps: list[ApprovalMatrixStep] = []
	for row in loaded:
		if not isinstance(row, dict) or set(row) - allowed:
			raise ApprovalMatrixContractError("an approval step contains unsupported fields")
		for required in ("sequence_no", "step_name", "threshold_amount", "approver_type"):
			if required not in row:
				raise ApprovalMatrixContractError(f"an approval step is missing {required}")
		steps.append(build_approval_matrix_step(**row, schema_version=version, amount_precision=precision))
	_validate_step_chain(tuple(steps))
	return tuple(steps)


def _validate_step_chain(steps: tuple[ApprovalMatrixStep, ...]) -> None:
	if not steps or len(steps) > MAX_APPROVAL_STEPS:
		raise ApprovalMatrixContractError("approval step count is outside the allowed range")
	sequences = sorted({step.sequence_no for step in steps})
	if sequences != list(range(1, len(sequences) + 1)):
		raise ApprovalMatrixContractError("approval step sequences must be contiguous from one")
	previous_threshold = Decimal("0")
	seen: set[tuple[int, str, str]] = set()
	for sequence in sequences:
		rows = tuple(step for step in steps if step.sequence_no == sequence)
		thresholds = {step.threshold_amount for step in rows}
		modes = {step.approval_mode for step in rows}
		names = {step.step_name for step in rows}
		if len(thresholds) != 1 or len(modes) != 1 or len(names) != 1:
			raise ApprovalMatrixContractError("parallel approvers must share step policy")
		threshold = next(iter(thresholds))
		if sequence == 1 and threshold != 0:
			raise ApprovalMatrixContractError("the first approval step must start at zero")
		if threshold < previous_threshold:
			raise ApprovalMatrixContractError("approval thresholds must be nondecreasing")
		previous_threshold = threshold
		for step in rows:
			key = (sequence, step.approver_type, step.source_value)
			if key in seen:
				raise ApprovalMatrixContractError("an approver cannot repeat within a step")
			seen.add(key)


@dataclass(frozen=True, slots=True)
class ApprovalMatrixDefinition:
	code: str
	display_name: str
	target_doctype: str
	company: str
	hospital: str
	organization_unit: str | None
	include_descendants: bool
	priority: int
	dimensions: dict[str, str]
	enabled: bool
	valid_from: str
	valid_to: str | None
	revision: int
	remarks: str | None
	steps: tuple[ApprovalMatrixStep, ...]
	policy_digest: str
	schema_version: int = 1
	amount_precision: int = 2

	def as_public_dict(self) -> dict[str, object]:
		payload: dict[str, object] = {
			"schema_version": self.schema_version,
			"code": self.code,
			"display_name": self.display_name,
			"target_doctype": self.target_doctype,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit,
			"include_descendants": self.include_descendants,
			"priority": self.priority,
			"dimensions": dict(self.dimensions),
			"enabled": self.enabled,
			"valid_from": self.valid_from,
			"valid_to": self.valid_to,
			"revision": self.revision,
			"remarks": self.remarks,
			"steps": [step.as_dict() for step in self.steps],
			"policy_digest": self.policy_digest,
		}
		if self.schema_version == EXACT_APPROVAL_SCHEMA_VERSION:
			payload["amount_precision"] = self.amount_precision
		return payload


def build_approval_matrix_definition(
	*,
	code: object,
	display_name: object,
	target_doctype: object,
	company: object,
	hospital: object,
	organization_unit: object = None,
	include_descendants: object = False,
	priority: object = 0,
	dimensions: object = None,
	enabled: object = True,
	valid_from: object,
	valid_to: object = None,
	revision: object = 1,
	remarks: object = None,
	steps: tuple[ApprovalMatrixStep, ...] | list[ApprovalMatrixStep],
	schema_version: object = 1,
	amount_precision: object = 2,
) -> ApprovalMatrixDefinition:
	try:
		version, precision = approval_amount_profile(schema_version, amount_precision)
		start = normalize_required_date(valid_from, label="valid_from")
		end = normalize_optional_date(valid_to, label="valid_to")
		validate_date_range(start, end)
		normalized_steps = tuple(steps)
		_validate_step_chain(normalized_steps)
		if any(
			(step.schema_version, step.amount_precision) != (version, precision) for step in normalized_steps
		):
			raise ApprovalMatrixContractError("approval step amount profile differs from its matrix")
		normalized_unit = (
			normalize_reference(organization_unit, label="organization_unit") if organization_unit else None
		)
		descendants = normalize_boolean(include_descendants, label="include_descendants")
		if descendants and normalized_unit is None:
			raise ApprovalMatrixContractError("include_descendants requires organization_unit")
		normalized_priority = normalize_nonnegative_integer(priority, label="priority")
		if normalized_priority > 9999:
			raise ApprovalMatrixContractError("priority is outside the allowed range")
		normalized_dimensions = _load_dimensions(dimensions, context=False)
		payload: dict[str, object] = {
			"schema_version": version,
			"code": normalize_code(code, label="code"),
			"display_name": normalize_required_text(display_name, label="display_name"),
			"target_doctype": normalize_reference(target_doctype, label="target_doctype"),
			"company": normalize_reference(company, label="company"),
			"hospital": normalize_reference(hospital, label="hospital"),
			"organization_unit": normalized_unit,
			"include_descendants": descendants,
			"priority": normalized_priority,
			"dimensions": normalized_dimensions,
			"enabled": normalize_boolean(enabled, label="enabled"),
			"valid_from": start,
			"valid_to": end,
			"remarks": normalize_optional_text(remarks, label="remarks"),
			"steps": [
				step.as_dict()
				for step in sorted(
					normalized_steps,
					key=lambda item: (item.sequence_no, item.approver_type, item.source_value),
				)
			],
		}
		if version == EXACT_APPROVAL_SCHEMA_VERSION:
			payload["amount_precision"] = precision
		revision_value = normalize_positive_integer(revision, label="revision")
	except OrganizationContractError as exc:
		raise ApprovalMatrixContractError(str(exc)) from exc
	return ApprovalMatrixDefinition(
		code=str(payload["code"]),
		display_name=str(payload["display_name"]),
		target_doctype=str(payload["target_doctype"]),
		company=str(payload["company"]),
		hospital=str(payload["hospital"]),
		organization_unit=normalized_unit,
		include_descendants=descendants,
		priority=normalized_priority,
		dimensions=normalized_dimensions,
		enabled=bool(payload["enabled"]),
		valid_from=start,
		valid_to=end,
		revision=revision_value,
		remarks=payload["remarks"] if isinstance(payload["remarks"], str) else None,
		steps=normalized_steps,
		policy_digest=fingerprint_json(payload),
		schema_version=version,
		amount_precision=precision,
	)


@dataclass(frozen=True, slots=True)
class ApprovalMatrixUpsert:
	matrix_name: str | None
	definition: ApprovalMatrixDefinition
	expected_revision: int

	def as_request_payload(self) -> dict[str, object]:
		return {
			"matrix_name": self.matrix_name,
			"expected_revision": self.expected_revision,
			**self.definition.as_public_dict(),
		}


def build_approval_matrix_upsert(
	*,
	code: object,
	display_name: object,
	target_doctype: object,
	company: object,
	hospital: object,
	valid_from: object,
	steps: tuple[ApprovalMatrixStep, ...] | list[ApprovalMatrixStep],
	organization_unit: object = None,
	include_descendants: object = False,
	priority: object = 0,
	dimensions: object = None,
	enabled: object = True,
	valid_to: object = None,
	remarks: object = None,
	matrix_name: object = None,
	expected_revision: object = 0,
	schema_version: object = 1,
	amount_precision: object = 2,
) -> ApprovalMatrixUpsert:
	try:
		expected = normalize_nonnegative_integer(expected_revision, label="expected_revision")
		name = normalize_reference(matrix_name, label="matrix_name") if matrix_name else None
	except OrganizationContractError as exc:
		raise ApprovalMatrixContractError(str(exc)) from exc
	return ApprovalMatrixUpsert(
		matrix_name=name,
		definition=build_approval_matrix_definition(
			code=code,
			display_name=display_name,
			target_doctype=target_doctype,
			company=company,
			hospital=hospital,
			organization_unit=organization_unit,
			include_descendants=include_descendants,
			priority=priority,
			dimensions=dimensions,
			enabled=enabled,
			valid_from=valid_from,
			valid_to=valid_to,
			revision=max(expected, 1),
			remarks=remarks,
			steps=steps,
			schema_version=schema_version,
			amount_precision=amount_precision,
		),
		expected_revision=expected,
	)


@dataclass(frozen=True, slots=True)
class ApprovalEvaluation:
	target_doctype: str
	docname: str
	amount: Decimal
	company: str
	hospital: str
	organization_unit: str
	effective_on: str
	dimensions: dict[str, str]
	schema_version: int = 1
	amount_precision: int = 2

	def as_request_payload(self) -> dict[str, object]:
		payload: dict[str, object] = {
			"doctype": self.target_doctype,
			"docname": self.docname,
			"amount": float(self.amount) if self.schema_version == 1 else canonical_decimal(self.amount),
			"dimensions": {
				"company": self.company,
				"hospital": self.hospital,
				"organization_unit": self.organization_unit,
				"effective_on": self.effective_on,
				**self.dimensions,
			},
		}
		if self.schema_version == EXACT_APPROVAL_SCHEMA_VERSION:
			payload.update(schema_version=self.schema_version, amount_precision=self.amount_precision)
		return payload


def build_approval_evaluation(
	*,
	doctype: object,
	docname: object,
	amount: object,
	dimensions: object,
	schema_version: object = 1,
	amount_precision: object = 2,
) -> ApprovalEvaluation:
	try:
		version, precision = approval_amount_profile(schema_version, amount_precision)
		target = normalize_reference(doctype, label="doctype")
		if target in EXACT_APPROVAL_TARGETS and version != EXACT_APPROVAL_SCHEMA_VERSION:
			raise ApprovalMatrixContractError("exact approval target requires the exact amount contract")
		loaded = _load_dimensions(dimensions, context=True)
		effective_on = normalize_required_date(loaded.pop("effective_on"), label="effective_on")
		return ApprovalEvaluation(
			target_doctype=target,
			docname=normalize_reference(docname, label="docname"),
			amount=_decimal(amount, label="amount", schema_version=version, amount_precision=precision),
			company=normalize_reference(loaded.pop("company"), label="company"),
			hospital=normalize_reference(loaded.pop("hospital"), label="hospital"),
			organization_unit=normalize_reference(
				loaded.pop("organization_unit"),
				label="organization_unit",
			),
			effective_on=effective_on,
			dimensions=loaded,
			schema_version=version,
			amount_precision=precision,
		)
	except OrganizationContractError as exc:
		raise ApprovalMatrixContractError(str(exc)) from exc


def active_steps(
	definition: ApprovalMatrixDefinition,
	amount: Decimal,
) -> tuple[ApprovalMatrixStep, ...]:
	return tuple(step for step in definition.steps if amount >= step.threshold_amount)


def build_approval_decision(
	*,
	evaluation: ApprovalEvaluation,
	matrix: str,
	definition: ApprovalMatrixDefinition,
	resolved_rows: list[tuple[ApprovalMatrixStep, list[ResolvedApprover]]],
) -> ApprovalDecision:
	if definition.schema_version == EXACT_APPROVAL_SCHEMA_VERSION and (
		evaluation.schema_version != EXACT_APPROVAL_SCHEMA_VERSION
		or evaluation.amount_precision != definition.amount_precision
	):
		raise ApprovalMatrixContractError("approval evaluation amount profile differs from its matrix")
	if evaluation.schema_version == EXACT_APPROVAL_SCHEMA_VERSION:
		expected = active_steps(definition, evaluation.amount)
		if len(expected) != len(resolved_rows) or set(expected) != {row for row, _ in resolved_rows}:
			raise ApprovalMatrixContractError("resolved approval sources differ from the active policy")
	grouped: dict[int, ResolvedApprovalStep] = {}
	for row, approvers in resolved_rows:
		step = grouped.setdefault(
			row.sequence_no,
			{
				"sequence_no": row.sequence_no,
				"step_name": row.step_name,
				"approval_mode": row.approval_mode,
				"threshold_amount": (
					float(row.threshold_amount)
					if evaluation.schema_version == 1
					else canonical_decimal(row.threshold_amount)
				),
				"approvers": [],
			},
		)
		for approver in approvers:
			if evaluation.schema_version == EXACT_APPROVAL_SCHEMA_VERSION:
				step.setdefault("source_assignments", []).append(approver.copy())
			existing = next(
				(item for item in step["approvers"] if item["user"] == approver["user"]),
				None,
			)
			if existing is None:
				step["approvers"].append(approver.copy() if evaluation.schema_version == 2 else approver)
				continue
			if (approver["source_type"], approver["source_value"]) < (
				existing["source_type"],
				existing["source_value"],
			):
				if evaluation.schema_version == 2:
					step["approvers"][step["approvers"].index(existing)] = approver.copy()
				else:
					existing.update(approver)
	for step in grouped.values():
		step["approvers"].sort(key=lambda item: (item["user"], item["source_type"], item["source_value"]))
		if evaluation.schema_version == EXACT_APPROVAL_SCHEMA_VERSION:
			step.setdefault("source_assignments", []).sort(
				key=lambda item: (
					item["user"],
					item["source_type"],
					item["source_value"],
					item.get("delegated_from", ""),
					item.get("delegation", ""),
				)
			)
		if not step["approvers"]:
			raise ApprovalMatrixContractError("an active approval step has no approver")
	steps = [grouped[key] for key in sorted(grouped)]
	if [step["sequence_no"] for step in steps] != list(range(1, len(steps) + 1)):
		raise ApprovalMatrixContractError("active approval steps are not contiguous")
	approvers = sorted({approver["user"] for step in steps for approver in step["approvers"]})
	delegations = sorted(
		{
			str(delegation)
			for step in steps
			for approver in step.get("source_assignments", step["approvers"])
			if (delegation := approver.get("delegation"))
		}
	)
	decision: ApprovalDecision = {
		"schema_version": evaluation.schema_version,
		"doctype": evaluation.target_doctype,
		"docname": evaluation.docname,
		"matrix": matrix,
		"matrix_code": definition.code,
		"matrix_name": definition.display_name,
		"policy_version": definition.revision,
		"policy_digest": definition.policy_digest,
		"approvers": approvers,
		"delegations": delegations,
		"steps": steps,
		"decision_digest": "",
	}
	if evaluation.schema_version == EXACT_APPROVAL_SCHEMA_VERSION:
		decision.update(
			amount=canonical_decimal(evaluation.amount),
			amount_precision=evaluation.amount_precision,
			context_digest=fingerprint_json(evaluation.as_request_payload()),
			policy_schema_version=definition.schema_version,
		)
	decision["decision_digest"] = fingerprint_json(decision)
	return decision


__all__ = [
	"APPROVAL_MATRIX_DOCTYPE",
	"APPROVAL_MATRIX_ROW_DOCTYPE",
	"APPROVAL_MATRIX_SCHEMA_VERSION",
	"APPROVAL_MODES",
	"APPROVER_TYPES",
	"CONTEXT_DIMENSIONS",
	"EXACT_APPROVAL_SCHEMA_VERSION",
	"EXACT_APPROVAL_TARGETS",
	"MAX_APPROVAL_STEPS",
	"MAX_DIMENSIONS",
	"ApprovalDecision",
	"ApprovalEvaluation",
	"ApprovalMatrixContractError",
	"ApprovalMatrixDefinition",
	"ApprovalMatrixStep",
	"ApprovalMatrixUpsert",
	"DelegatedApproverEvidence",
	"ResolvedApprover",
	"active_steps",
	"approval_amount_profile",
	"build_approval_decision",
	"build_approval_evaluation",
	"build_approval_matrix_definition",
	"build_approval_matrix_step",
	"build_approval_matrix_steps",
	"build_approval_matrix_upsert",
]
