from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from typing import Literal

from medops.common.domain_service import canonical_json, fingerprint_json
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

NUMBERING_SCHEMA_VERSION = 1
NUMBERING_SCHEME_DOCTYPE = "HRP Numbering Scheme"
NUMBER_RESERVATION_DOCTYPE = "HRP Number Reservation"
MAX_TEMPLATE_LENGTH = 120
MAX_RENDERED_NUMBER_LENGTH = 140
MAX_DIMENSIONS = 12
MAX_DIMENSIONS_BYTES = 4096
MIN_SEQUENCE_DIGITS = 2
MAX_SEQUENCE_DIGITS = 12
RESET_POLICIES = ("Never", "Yearly", "Monthly", "Daily")
ResetPolicy = Literal["Never", "Yearly", "Monthly", "Daily"]

_TOKEN_PATTERN = re.compile(r"\{(YYYY|YY|MM|DD|SEQ|DIM:[a-z][a-z0-9_]{0,31})\}")
_LITERAL_PATTERN = re.compile(r"^[A-Z0-9._/-]*$")
_DIMENSION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_DIMENSION_VALUE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")
_RESERVATION_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{15,63}$")


class NumberingContractError(ValueError):
	"""Raised when a numbering command violates the public contract."""


def normalize_template(value: object) -> str:
	try:
		template = normalize_required_text(value, label="template", maximum=MAX_TEMPLATE_LENGTH)
	except OrganizationContractError as exc:
		raise NumberingContractError(str(exc)) from exc
	tokens = tuple(match.group(1) for match in _TOKEN_PATTERN.finditer(template))
	if tokens.count("SEQ") != 1:
		raise NumberingContractError("template must contain exactly one SEQ token")

	literals = _TOKEN_PATTERN.sub("", template)
	if "{" in literals or "}" in literals or _LITERAL_PATTERN.fullmatch(literals) is None:
		raise NumberingContractError("template contains an unsupported token or literal")
	return template


def dimension_keys_for(template: str) -> tuple[str, ...]:
	keys = tuple(
		token.removeprefix("DIM:")
		for token in (match.group(1) for match in _TOKEN_PATTERN.finditer(template))
		if token.startswith("DIM:")
	)
	if len(keys) > MAX_DIMENSIONS or len(set(keys)) != len(keys):
		raise NumberingContractError("template dimension keys must be unique and bounded")
	return keys


def normalize_reset_policy(value: object) -> ResetPolicy:
	if value not in RESET_POLICIES:
		raise NumberingContractError("reset_policy is invalid")
	return value


def normalize_sequence_digits(value: object) -> int:
	digits = normalize_positive_integer(value, label="sequence_digits")
	if not MIN_SEQUENCE_DIGITS <= digits <= MAX_SEQUENCE_DIGITS:
		raise NumberingContractError("sequence_digits is outside the allowed range")
	return digits


def normalize_start_number(value: object, *, sequence_digits: int) -> int:
	start_number = normalize_positive_integer(value, label="start_number")
	if start_number > (10**sequence_digits) - 1:
		raise NumberingContractError("start_number does not fit sequence_digits")
	return start_number


def _load_dimensions(value: object) -> dict[str, object]:
	loaded = value
	if isinstance(value, str):
		if len(value.encode("utf-8")) > MAX_DIMENSIONS_BYTES:
			raise NumberingContractError("dimensions payload is too large")
		try:
			loaded = json.loads(value)
		except json.JSONDecodeError as exc:
			raise NumberingContractError("dimensions must contain valid JSON") from exc
	if not isinstance(loaded, dict):
		raise NumberingContractError("dimensions must be an object")
	try:
		serialized = canonical_json(loaded)
	except ValueError as exc:
		raise NumberingContractError("dimensions must contain safe JSON values") from exc
	if len(serialized.encode("utf-8")) > MAX_DIMENSIONS_BYTES:
		raise NumberingContractError("dimensions payload is too large")
	return loaded


def normalize_dimensions(
	value: object,
	*,
	required_keys: tuple[str, ...] | None = None,
) -> dict[str, str]:
	loaded = _load_dimensions(value)
	if len(loaded) > MAX_DIMENSIONS:
		raise NumberingContractError("dimensions count exceeds the allowed range")
	if required_keys is not None and set(loaded) != set(required_keys):
		raise NumberingContractError("dimensions do not match the scheme template")

	normalized: dict[str, str] = {}
	for key in sorted(loaded):
		if _DIMENSION_KEY_PATTERN.fullmatch(key) is None:
			raise NumberingContractError("dimension key is invalid")
		value_item = loaded[key]
		if not isinstance(value_item, str) or value_item != value_item.strip():
			raise NumberingContractError("dimension value is invalid")
		normalized_value = value_item.upper()
		if _DIMENSION_VALUE_PATTERN.fullmatch(normalized_value) is None:
			raise NumberingContractError("dimension value is invalid")
		normalized[key] = normalized_value
	return normalized


def normalize_reservation_token(value: object) -> str:
	if not isinstance(value, str) or _RESERVATION_TOKEN_PATTERN.fullmatch(value) is None:
		raise NumberingContractError("reservation_token is invalid")
	return value


def template_digest_for(
	*,
	template: str,
	reset_policy: ResetPolicy,
	sequence_digits: int,
	start_number: int,
) -> str:
	return fingerprint_json(
		{
			"schema_version": NUMBERING_SCHEMA_VERSION,
			"template": template,
			"reset_policy": reset_policy,
			"sequence_digits": sequence_digits,
			"start_number": start_number,
		}
	)


def dimensions_digest_for(dimensions: dict[str, str]) -> str:
	return fingerprint_json(dimensions)


def reset_bucket_for(reset_policy: ResetPolicy, business_date: str) -> str:
	parsed = date.fromisoformat(business_date)
	if reset_policy == "Never":
		return "ALL"
	if reset_policy == "Yearly":
		return parsed.strftime("%Y")
	if reset_policy == "Monthly":
		return parsed.strftime("%Y%m")
	return parsed.strftime("%Y%m%d")


def counter_key_for(
	*,
	scheme: str,
	reset_policy: ResetPolicy,
	business_date: str,
	dimensions: dict[str, str],
) -> str:
	digest = fingerprint_json(
		{
			"schema_version": NUMBERING_SCHEMA_VERSION,
			"scheme": scheme,
			"bucket": reset_bucket_for(reset_policy, business_date),
			"dimensions": dimensions,
		}
	)
	return f"IONE-HRP-NUM-{digest[:48]}"


def render_number(
	*,
	template: str,
	business_date: str,
	dimensions: dict[str, str],
	sequence_value: int,
	sequence_digits: int,
) -> str:
	if sequence_value < 1 or sequence_value > (10**sequence_digits) - 1:
		raise NumberingContractError("numbering sequence is exhausted")
	parsed = date.fromisoformat(business_date)
	values = {
		"YYYY": parsed.strftime("%Y"),
		"YY": parsed.strftime("%y"),
		"MM": parsed.strftime("%m"),
		"DD": parsed.strftime("%d"),
		"SEQ": f"{sequence_value:0{sequence_digits}d}",
	}

	def replace(match: re.Match[str]) -> str:
		token = match.group(1)
		if token.startswith("DIM:"):
			return dimensions[token.removeprefix("DIM:")]
		return values[token]

	number = _TOKEN_PATTERN.sub(replace, template)
	if not number or len(number) > MAX_RENDERED_NUMBER_LENGTH:
		raise NumberingContractError("rendered number is outside the allowed range")
	return number


@dataclass(frozen=True, slots=True)
class NumberingSchemeUpsert:
	scheme_name: str | None
	code: str
	display_name: str
	company: str
	hospital: str
	organization_unit: str | None
	template: str
	reset_policy: ResetPolicy
	sequence_digits: int
	start_number: int
	enabled: bool
	valid_from: str | None
	valid_to: str | None
	expected_revision: int
	remarks: str | None
	dimension_keys: tuple[str, ...]
	template_digest: str

	def as_request_payload(self) -> dict[str, object]:
		return {
			"scheme_name": self.scheme_name,
			"code": self.code,
			"display_name": self.display_name,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit,
			"template": self.template,
			"reset_policy": self.reset_policy,
			"sequence_digits": self.sequence_digits,
			"start_number": self.start_number,
			"enabled": self.enabled,
			"valid_from": self.valid_from,
			"valid_to": self.valid_to,
			"expected_revision": self.expected_revision,
			"remarks": self.remarks,
		}


def build_numbering_scheme_upsert(
	*,
	scheme_name: object = None,
	code: object,
	display_name: object,
	company: object,
	hospital: object,
	organization_unit: object = None,
	template: object,
	reset_policy: object,
	sequence_digits: object,
	start_number: object = 1,
	enabled: object = True,
	valid_from: object = None,
	valid_to: object = None,
	expected_revision: object = 0,
	remarks: object = None,
) -> NumberingSchemeUpsert:
	try:
		normalized_template = normalize_template(template)
		normalized_policy = normalize_reset_policy(reset_policy)
		normalized_digits = normalize_sequence_digits(sequence_digits)
		normalized_start = normalize_start_number(start_number, sequence_digits=normalized_digits)
		start = normalize_optional_date(valid_from, label="valid_from")
		end = normalize_optional_date(valid_to, label="valid_to")
		validate_date_range(start, end)
		keys = dimension_keys_for(normalized_template)
		return NumberingSchemeUpsert(
			scheme_name=(normalize_reference(scheme_name, label="scheme_name") if scheme_name else None),
			code=normalize_code(code, label="code"),
			display_name=normalize_required_text(display_name, label="display_name"),
			company=normalize_reference(company, label="company"),
			hospital=normalize_code(hospital, label="hospital"),
			organization_unit=(
				normalize_reference(organization_unit, label="organization_unit")
				if organization_unit
				else None
			),
			template=normalized_template,
			reset_policy=normalized_policy,
			sequence_digits=normalized_digits,
			start_number=normalized_start,
			enabled=normalize_boolean(enabled, label="enabled"),
			valid_from=start,
			valid_to=end,
			expected_revision=normalize_nonnegative_integer(
				expected_revision,
				label="expected_revision",
			),
			remarks=normalize_optional_text(remarks, label="remarks"),
			dimension_keys=keys,
			template_digest=template_digest_for(
				template=normalized_template,
				reset_policy=normalized_policy,
				sequence_digits=normalized_digits,
				start_number=normalized_start,
			),
		)
	except OrganizationContractError as exc:
		raise NumberingContractError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class NumberAllocation:
	scheme: str
	dimensions: dict[str, str]
	business_date: str

	def as_request_payload(self) -> dict[str, object]:
		return {
			"scheme": self.scheme,
			"dimensions": self.dimensions,
			"date": self.business_date,
		}


def build_number_allocation(
	*,
	scheme: object,
	dimensions: object,
	business_date: object,
	required_dimension_keys: tuple[str, ...] | None = None,
) -> NumberAllocation:
	try:
		return NumberAllocation(
			scheme=normalize_code(scheme, label="scheme"),
			dimensions=normalize_dimensions(
				dimensions,
				required_keys=required_dimension_keys,
			),
			business_date=normalize_required_date(business_date, label="date"),
		)
	except OrganizationContractError as exc:
		raise NumberingContractError(str(exc)) from exc


def canonical_dimension_keys(keys: tuple[str, ...]) -> str:
	return canonical_json(list(keys))


def reservation_name_for(token: str) -> str:
	normalized = normalize_reservation_token(token)
	return f"num-{sha256(normalized.encode()).hexdigest()}"


__all__ = [
	"MAX_DIMENSIONS",
	"MAX_RENDERED_NUMBER_LENGTH",
	"MAX_SEQUENCE_DIGITS",
	"MAX_TEMPLATE_LENGTH",
	"MIN_SEQUENCE_DIGITS",
	"NUMBERING_SCHEMA_VERSION",
	"NUMBERING_SCHEME_DOCTYPE",
	"NUMBER_RESERVATION_DOCTYPE",
	"RESET_POLICIES",
	"NumberAllocation",
	"NumberingContractError",
	"NumberingSchemeUpsert",
	"build_number_allocation",
	"build_numbering_scheme_upsert",
	"canonical_dimension_keys",
	"counter_key_for",
	"dimension_keys_for",
	"dimensions_digest_for",
	"normalize_dimensions",
	"normalize_reservation_token",
	"normalize_reset_policy",
	"normalize_sequence_digits",
	"normalize_start_number",
	"normalize_template",
	"render_number",
	"reservation_name_for",
	"reset_bucket_for",
	"template_digest_for",
]
