from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, cast

from medops.common.domain_service import canonical_json, fingerprint_json
from medops.common.master_data import (
	FIELD_NAME_PATTERN,
	SENSITIVE_FIELD_TOKENS,
	MasterDataFieldPolicy,
	MasterDataTargetPolicy,
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

DATA_QUALITY_SCHEMA_VERSION = 1
DATA_QUALITY_RULE_DOCTYPE = "HRP Data Quality Rule"
DATA_QUALITY_ISSUE_DOCTYPE = "HRP Data Quality Issue"
MAX_RULE_PARAMETERS_BYTES = 8 * 1024
MAX_ALLOWED_VALUES = 64
MAX_TEXT_RULE_LENGTH = 500
RULE_TYPES = (
	"Required",
	"Allowed Values",
	"Maximum Length",
	"Named Pattern",
	"Reference Exists",
)
SEVERITIES = ("Critical", "Major", "Minor")
ISSUE_STATUSES = ("Open", "Resolved")
RuleType = Literal[
	"Required",
	"Allowed Values",
	"Maximum Length",
	"Named Pattern",
	"Reference Exists",
]
Severity = Literal["Critical", "Major", "Minor"]

NAMED_PATTERNS: dict[str, re.Pattern[str]] = {
	"UPPER_CODE": re.compile(r"[A-Z][A-Z0-9._/-]{0,139}"),
	"EMAIL": re.compile(
		r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
	),
	"CN_MOBILE": re.compile(r"1[3-9][0-9]{9}"),
	"NUMERIC": re.compile(r"[0-9]+(?:\.[0-9]+)?"),
	"ALPHANUMERIC": re.compile(r"[A-Za-z0-9]+"),
}


class DataQualityContractError(ValueError):
	"""Raised when a data-quality command violates the public contract."""


def _normalize_target_field(value: object) -> str:
	if (
		not isinstance(value, str)
		or value != value.strip()
		or FIELD_NAME_PATTERN.fullmatch(value) is None
		or value in SENSITIVE_FIELD_TOKENS
		or any(token in value for token in ("password", "secret", "token"))
	):
		raise DataQualityContractError("target_field is invalid")
	return value


def _load_parameter_object(value: object) -> dict[str, object]:
	loaded = value
	if value in (None, ""):
		loaded = {}
	elif isinstance(value, str):
		if len(value.encode("utf-8")) > MAX_RULE_PARAMETERS_BYTES:
			raise DataQualityContractError("parameters payload is too large")
		try:
			loaded = json.loads(value)
		except json.JSONDecodeError as exc:
			raise DataQualityContractError("parameters must contain valid JSON") from exc
	if not isinstance(loaded, dict):
		raise DataQualityContractError("parameters must be an object")
	try:
		serialized = canonical_json(loaded)
	except ValueError as exc:
		raise DataQualityContractError("parameters are invalid") from exc
	if len(serialized.encode("utf-8")) > MAX_RULE_PARAMETERS_BYTES:
		raise DataQualityContractError("parameters payload is too large")
	return cast(dict[str, object], json.loads(serialized))


def normalize_rule_parameters(rule_type: object, value: object) -> tuple[RuleType, str]:
	if not isinstance(rule_type, str) or rule_type not in RULE_TYPES:
		raise DataQualityContractError("rule_type is invalid")
	normalized_type = cast(RuleType, rule_type)
	parameters = _load_parameter_object(value)

	if normalized_type in {"Required", "Reference Exists"}:
		if parameters:
			raise DataQualityContractError("rule_type does not accept parameters")
		normalized: dict[str, object] = {}
	elif normalized_type == "Allowed Values":
		if set(parameters) != {"values"}:
			raise DataQualityContractError("Allowed Values requires only values")
		values = parameters["values"]
		if not isinstance(values, list) or not 1 <= len(values) <= MAX_ALLOWED_VALUES:
			raise DataQualityContractError("values count is outside the allowed range")
		normalized_values: list[str] = []
		for raw_value in values:
			try:
				normalized_value = normalize_required_text(
					raw_value,
					label="allowed_value",
					maximum=140,
				)
			except OrganizationContractError as exc:
				raise DataQualityContractError(str(exc)) from exc
			normalized_values.append(normalized_value)
		if len(set(normalized_values)) != len(normalized_values):
			raise DataQualityContractError("values must be unique")
		normalized = {"values": sorted(normalized_values)}
	elif normalized_type == "Maximum Length":
		if set(parameters) != {"maximum"}:
			raise DataQualityContractError("Maximum Length requires only maximum")
		try:
			maximum = normalize_positive_integer(parameters["maximum"], label="maximum")
		except OrganizationContractError as exc:
			raise DataQualityContractError(str(exc)) from exc
		if maximum > MAX_TEXT_RULE_LENGTH:
			raise DataQualityContractError("maximum is outside the allowed range")
		normalized = {"maximum": maximum}
	else:
		if set(parameters) != {"pattern_name"}:
			raise DataQualityContractError("Named Pattern requires only pattern_name")
		pattern_name = parameters["pattern_name"]
		if not isinstance(pattern_name, str) or pattern_name not in NAMED_PATTERNS:
			raise DataQualityContractError("pattern_name is invalid")
		normalized = {"pattern_name": pattern_name}
	return normalized_type, canonical_json(normalized)


@dataclass(frozen=True, slots=True)
class DataQualityRuleUpsert:
	rule_name: str | None
	code: str
	display_name: str
	master_data_domain: str
	company: str
	hospital: str
	organization_unit: str | None
	target_field: str
	rule_type: RuleType
	parameters_json: str
	severity: Severity
	enabled: bool
	valid_from: str
	valid_to: str | None
	expected_revision: int
	remarks: str | None

	@property
	def parameters(self) -> dict[str, object]:
		return cast(dict[str, object], json.loads(self.parameters_json))

	@property
	def rule_digest(self) -> str:
		return fingerprint_json(
			{
				"schema_version": DATA_QUALITY_SCHEMA_VERSION,
				"code": self.code,
				"master_data_domain": self.master_data_domain,
				"company": self.company,
				"hospital": self.hospital,
				"organization_unit": self.organization_unit,
				"target_field": self.target_field,
				"rule_type": self.rule_type,
				"parameters": self.parameters,
				"severity": self.severity,
				"valid_from": self.valid_from,
				"valid_to": self.valid_to,
			}
		)

	def as_request_payload(self) -> dict[str, object]:
		return {
			"rule_name": self.rule_name,
			"code": self.code,
			"display_name": self.display_name,
			"master_data_domain": self.master_data_domain,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit,
			"target_field": self.target_field,
			"rule_type": self.rule_type,
			"parameters": self.parameters,
			"severity": self.severity,
			"enabled": self.enabled,
			"valid_from": self.valid_from,
			"valid_to": self.valid_to,
			"expected_revision": self.expected_revision,
			"remarks_digest": fingerprint_json({"remarks": self.remarks}),
			"rule_digest": self.rule_digest,
		}


def build_data_quality_rule_upsert(
	*,
	code: object,
	display_name: object,
	master_data_domain: object,
	company: object,
	hospital: object,
	target_field: object,
	rule_type: object,
	parameters: object = None,
	severity: object = "Major",
	enabled: object = True,
	valid_from: object,
	valid_to: object = None,
	organization_unit: object = None,
	expected_revision: object = 0,
	rule_name: object = None,
	remarks: object = None,
) -> DataQualityRuleUpsert:
	try:
		normalized_rule_type, parameters_json = normalize_rule_parameters(rule_type, parameters)
		normalized_severity = severity
		if not isinstance(normalized_severity, str) or normalized_severity not in SEVERITIES:
			raise DataQualityContractError("severity is invalid")
		normalized_name = (
			normalize_reference(rule_name, label="rule_name") if rule_name not in (None, "") else None
		)
		normalized_revision = normalize_nonnegative_integer(
			expected_revision,
			label="expected_revision",
		)
		if (normalized_name is None) != (normalized_revision == 0):
			raise DataQualityContractError("rule_name and positive expected_revision are required together")
		normalized_valid_from = normalize_required_date(valid_from, label="valid_from")
		normalized_valid_to = normalize_optional_date(valid_to, label="valid_to")
		validate_date_range(normalized_valid_from, normalized_valid_to)
		return DataQualityRuleUpsert(
			rule_name=normalized_name,
			code=normalize_code(code, label="code"),
			display_name=normalize_required_text(display_name, label="display_name"),
			master_data_domain=normalize_code(
				master_data_domain,
				label="master_data_domain",
			),
			company=normalize_reference(company, label="company"),
			hospital=normalize_code(hospital, label="hospital"),
			organization_unit=(
				normalize_reference(organization_unit, label="organization_unit")
				if organization_unit not in (None, "")
				else None
			),
			target_field=_normalize_target_field(target_field),
			rule_type=normalized_rule_type,
			parameters_json=parameters_json,
			severity=cast(Severity, normalized_severity),
			enabled=normalize_boolean(enabled, label="enabled"),
			valid_from=normalized_valid_from,
			valid_to=normalized_valid_to,
			expected_revision=normalized_revision,
			remarks=normalize_optional_text(remarks, label="remarks"),
		)
	except (OrganizationContractError, DataQualityContractError) as exc:
		if isinstance(exc, DataQualityContractError):
			raise
		raise DataQualityContractError(str(exc)) from exc


def validate_rule_for_policy(
	command: DataQualityRuleUpsert,
	policy: MasterDataTargetPolicy,
) -> MasterDataFieldPolicy:
	field = policy.fields_by_name.get(command.target_field)
	if field is None:
		raise DataQualityContractError("target_field is not allowed by the master-data policy")
	if command.rule_type == "Maximum Length" and field.value_type != "Text":
		raise DataQualityContractError("Maximum Length requires a text field")
	if command.rule_type == "Named Pattern" and field.value_type != "Text":
		raise DataQualityContractError("Named Pattern requires a text field")
	if command.rule_type == "Reference Exists" and field.value_type != "Link":
		raise DataQualityContractError("Reference Exists requires a link field")
	return field


@dataclass(frozen=True, slots=True)
class DataQualityEvaluate:
	rule_name: str
	target_name: str
	effective_on: str
	expected_rule_revision: int

	def as_request_payload(self) -> dict[str, object]:
		return {
			"rule_name": self.rule_name,
			"target_name": self.target_name,
			"effective_on": self.effective_on,
			"expected_rule_revision": self.expected_rule_revision,
		}


def build_data_quality_evaluate(
	*,
	rule_name: object,
	target_name: object,
	effective_on: object,
	expected_rule_revision: object,
) -> DataQualityEvaluate:
	try:
		return DataQualityEvaluate(
			rule_name=normalize_reference(rule_name, label="rule_name"),
			target_name=normalize_reference(target_name, label="target_name"),
			effective_on=normalize_required_date(effective_on, label="effective_on"),
			expected_rule_revision=normalize_positive_integer(
				expected_rule_revision,
				label="expected_rule_revision",
			),
		)
	except OrganizationContractError as exc:
		raise DataQualityContractError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class QualityOutcome:
	passed: bool
	failure_code: str | None
	failure_message: str | None


def _value_present(value: object) -> bool:
	return value is not None and (not isinstance(value, str) or bool(value.strip()))


def evaluate_quality_value(
	*,
	rule_type: RuleType,
	parameters_json: str,
	value: object,
	reference_exists: bool | None = None,
) -> QualityOutcome:
	parameters = cast(dict[str, object], json.loads(parameters_json))
	if rule_type == "Required":
		passed = _value_present(value)
		code, message = "REQUIRED_MISSING", "必填值缺失"
	elif rule_type == "Allowed Values":
		passed = not _value_present(value) or str(value) in cast(list[str], parameters["values"])
		code, message = "VALUE_NOT_ALLOWED", "值不在允许范围内"
	elif rule_type == "Maximum Length":
		maximum = cast(int, parameters["maximum"])
		passed = not _value_present(value) or len(str(value)) <= maximum
		code, message = "MAXIMUM_LENGTH_EXCEEDED", "文本长度超过上限"
	elif rule_type == "Named Pattern":
		pattern = NAMED_PATTERNS[str(parameters["pattern_name"])]
		passed = not _value_present(value) or pattern.fullmatch(str(value)) is not None
		code, message = "NAMED_PATTERN_MISMATCH", "值不符合命名格式"
	else:
		passed = not _value_present(value) or reference_exists is True
		code, message = "REFERENCE_NOT_FOUND", "引用记录不存在"
	return QualityOutcome(
		passed=passed,
		failure_code=None if passed else code,
		failure_message=None if passed else message,
	)


def issue_key_for(*, rule_name: str, target_doctype: str, target_name: str) -> str:
	return fingerprint_json(
		{
			"schema_version": DATA_QUALITY_SCHEMA_VERSION,
			"rule_name": rule_name,
			"target_doctype": target_doctype,
			"target_name": target_name,
		}
	)


def observed_value_digest(value: object) -> str:
	return fingerprint_json({"schema_version": DATA_QUALITY_SCHEMA_VERSION, "value": value})


__all__ = [
	"DATA_QUALITY_ISSUE_DOCTYPE",
	"DATA_QUALITY_RULE_DOCTYPE",
	"DATA_QUALITY_SCHEMA_VERSION",
	"ISSUE_STATUSES",
	"NAMED_PATTERNS",
	"RULE_TYPES",
	"SEVERITIES",
	"DataQualityContractError",
	"DataQualityEvaluate",
	"DataQualityRuleUpsert",
	"QualityOutcome",
	"build_data_quality_evaluate",
	"build_data_quality_rule_upsert",
	"evaluate_quality_value",
	"issue_key_for",
	"normalize_rule_parameters",
	"observed_value_digest",
	"validate_rule_for_policy",
]
