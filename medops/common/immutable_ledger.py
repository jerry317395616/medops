from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, localcontext
from types import MappingProxyType
from typing import TypedDict

from medops.common.domain_service import (
	DomainServiceContractError,
	canonical_json_object,
	normalize_sha256,
)
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount, signed_decimal_amount

IMMUTABLE_LEDGER_SCHEMA_VERSION = 1
DOCTYPE_PATTERN = re.compile(r"^HRP [A-Za-z0-9][A-Za-z0-9 -]{1,134} Ledger$")
FIELDNAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
RESERVED_LEDGER_FIELDS = frozenset(
	{
		"doctype",
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"idx",
		"_user_tags",
		"_comments",
		"_assign",
		"_liked_by",
	}
)


class ImmutableLedgerContractError(DomainServiceContractError):
	"""Raised when an immutable ledger definition or entry violates the shared contract."""


class LedgerFieldPublicContract(TypedDict):
	fieldname: str
	fieldtype: str
	required: bool


class LedgerMutationPolicy(TypedDict):
	append_only: bool
	update: bool
	delete: bool
	rename: bool
	cancel: bool
	direct_insert: bool


class LedgerReversalPolicy(TypedDict):
	equal_and_opposite: bool
	requires_reversal_of: bool
	one_reversal_per_entry: bool
	row_lock: bool


class ImmutableLedgerPublicContract(TypedDict):
	schema_version: int
	base_fields: list[LedgerFieldPublicContract]
	mutation_policy: LedgerMutationPolicy
	reversal_policy: LedgerReversalPolicy
	http_write_enabled: bool


@dataclass(frozen=True, slots=True)
class LedgerFieldContract:
	fieldname: str
	fieldtype: str
	required: bool = False

	def as_public_dict(self) -> LedgerFieldPublicContract:
		return {
			"fieldname": self.fieldname,
			"fieldtype": self.fieldtype,
			"required": self.required,
		}


BASE_LEDGER_FIELDS = (
	LedgerFieldContract("company", "Link", True),
	LedgerFieldContract("organization_unit", "Link", True),
	LedgerFieldContract("posting_date", "Date", True),
	LedgerFieldContract("posting_time", "Time", True),
	LedgerFieldContract("voucher_type", "Link", True),
	LedgerFieldContract("voucher_no", "Dynamic Link", True),
	LedgerFieldContract("reference_type", "Link"),
	LedgerFieldContract("reference_name", "Dynamic Link"),
	LedgerFieldContract("quantity", "Float"),
	LedgerFieldContract("debit", "Currency"),
	LedgerFieldContract("credit", "Currency"),
	LedgerFieldContract("amount", "Currency"),
	LedgerFieldContract("currency", "Link"),
	LedgerFieldContract("is_reversal", "Check"),
	LedgerFieldContract("reversal_of", "Link"),
	LedgerFieldContract("dimensions_json", "Code"),
	LedgerFieldContract("source_hash", "Data"),
)
BASE_LEDGER_FIELDS_BY_NAME = MappingProxyType({field.fieldname: field for field in BASE_LEDGER_FIELDS})
EXACT_LEDGER_AMOUNT_FIELDS = frozenset({"amount", "debit", "credit"})


def _validate_fieldnames(values: tuple[str, ...] | frozenset[str], *, label: str) -> None:
	if any(
		not isinstance(fieldname, str)
		or FIELDNAME_PATTERN.fullmatch(fieldname) is None
		or fieldname in RESERVED_LEDGER_FIELDS
		for fieldname in values
	):
		raise ImmutableLedgerContractError(f"{label} contains an invalid fieldname")
	if len(set(values)) != len(values):
		raise ImmutableLedgerContractError(f"{label} contains duplicate fieldnames")


@dataclass(frozen=True, slots=True)
class ImmutableLedgerDefinition:
	doctype: str
	required_roles: frozenset[str]
	negated_fields: tuple[str, ...]
	swapped_field_pairs: tuple[tuple[str, str], ...] = ()
	reversal_override_fields: frozenset[str] = frozenset()
	required_reversal_override_fields: frozenset[str] = frozenset()
	amount_schema_version: int = 1

	def __post_init__(self) -> None:
		if type(self.amount_schema_version) is not int or self.amount_schema_version not in {1, 2}:
			raise ImmutableLedgerContractError("unsupported ledger amount schema")
		if not isinstance(self.doctype, str) or DOCTYPE_PATTERN.fullmatch(self.doctype) is None:
			raise ImmutableLedgerContractError("ledger doctype is invalid")
		if (
			not isinstance(self.required_roles, frozenset)
			or not self.required_roles
			or any(
				not isinstance(role, str) or not role or role != role.strip() or len(role) > 140
				for role in self.required_roles
			)
		):
			raise ImmutableLedgerContractError("ledger required_roles is invalid")
		if not isinstance(self.negated_fields, tuple) or not self.negated_fields:
			raise ImmutableLedgerContractError("ledger must declare at least one negated field")
		_validate_fieldnames(self.negated_fields, label="negated_fields")
		if not isinstance(self.swapped_field_pairs, tuple):
			raise ImmutableLedgerContractError("swapped_field_pairs is invalid")
		swapped_fields: list[str] = []
		for pair in self.swapped_field_pairs:
			if not isinstance(pair, tuple) or len(pair) != 2 or pair[0] == pair[1]:
				raise ImmutableLedgerContractError("swapped_field_pairs contains an invalid pair")
			swapped_fields.extend(pair)
		_validate_fieldnames(tuple(swapped_fields), label="swapped_field_pairs")
		if set(self.negated_fields).intersection(swapped_fields):
			raise ImmutableLedgerContractError("a field cannot be both negated and swapped")
		if not isinstance(self.reversal_override_fields, frozenset):
			raise ImmutableLedgerContractError("reversal_override_fields is invalid")
		if not isinstance(self.required_reversal_override_fields, frozenset):
			raise ImmutableLedgerContractError("required_reversal_override_fields is invalid")
		_validate_fieldnames(self.reversal_override_fields, label="reversal_override_fields")
		_validate_fieldnames(
			self.required_reversal_override_fields,
			label="required_reversal_override_fields",
		)
		if not self.required_reversal_override_fields.issubset(self.reversal_override_fields):
			raise ImmutableLedgerContractError(
				"required reversal overrides must be included in reversal_override_fields"
			)
		protected = {
			"is_reversal",
			"reversal_of",
			*self.negated_fields,
			*swapped_fields,
		}
		if self.amount_schema_version == 2:
			protected |= {
				"company",
				"organization_unit",
				"currency",
				"currency_precision",
				"amount_schema_version",
				"dimensions_json",
			}
		if self.reversal_override_fields.intersection(protected):
			raise ImmutableLedgerContractError("reversal overrides include a protected field")

	@property
	def transformed_fields(self) -> frozenset[str]:
		return frozenset(
			(
				*self.negated_fields,
				*(field for pair in self.swapped_field_pairs for field in pair),
			)
		)

	def as_public_dict(self) -> dict[str, object]:
		result: dict[str, object] = {
			"schema_version": IMMUTABLE_LEDGER_SCHEMA_VERSION,
			"doctype": self.doctype,
			"append_only": True,
			"reversal_only": True,
			"negated_fields": list(self.negated_fields),
			"swapped_field_pairs": [list(pair) for pair in self.swapped_field_pairs],
			"reversal_override_fields": sorted(self.reversal_override_fields),
		}
		if self.amount_schema_version == 2:
			result.update(
				schema_version=2, amount_schema_version=2, currency_precision_field="currency_precision"
			)
		return result


def ledger_field_contracts(definition: ImmutableLedgerDefinition) -> tuple[LedgerFieldContract, ...]:
	if definition.amount_schema_version == 1:
		return BASE_LEDGER_FIELDS
	return (
		*(
			LedgerFieldContract(field.fieldname, "Data", True)
			if field.fieldname in EXACT_LEDGER_AMOUNT_FIELDS
			else field
			for field in BASE_LEDGER_FIELDS
		),
		LedgerFieldContract("amount_schema_version", "Int", True),
		LedgerFieldContract("currency_precision", "Int", True),
	)


def normalize_exact_ledger_money(values: Mapping[str, object]) -> dict[str, object]:
	version = values.get("amount_schema_version")
	precision = values.get("currency_precision")
	if type(version) is not int or version != 2 or type(precision) is not int or not 0 <= precision <= 9:
		raise ImmutableLedgerContractError("exact ledger amount profile is required")
	try:
		with localcontext() as context:
			context.prec = 48
			positive = {
				key: exact_decimal_amount(values.get(key), precision, maximum=MAX_APPROVAL_AMOUNT)
				for key in ("debit", "credit")
			}
			return {
				"amount_schema_version": 2,
				"currency_precision": precision,
				"amount": signed_decimal_amount(values.get("amount"), precision),
				**{
					key: value.lstrip("-") if Decimal(value).is_zero() else value
					for key, value in positive.items()
				},
			}
	except ValueError as exc:
		raise ImmutableLedgerContractError("exact ledger amount is invalid") from exc


def normalize_dimensions_json(value: object) -> str:
	if value in (None, ""):
		return ""
	try:
		parsed = json.loads(value) if isinstance(value, str) else value
		_, serialized = canonical_json_object(parsed)
		return serialized
	except (json.JSONDecodeError, TypeError, DomainServiceContractError) as exc:
		raise ImmutableLedgerContractError("dimensions_json must be a JSON object") from exc


def normalize_optional_source_hash(value: object) -> str:
	if value in (None, ""):
		return ""
	try:
		return normalize_sha256(value, label="source_hash")
	except DomainServiceContractError as exc:
		raise ImmutableLedgerContractError("source_hash is invalid") from exc


def normalize_ledger_name(value: object, *, label: str = "entry_name") -> str:
	if (
		not isinstance(value, str)
		or not value
		or value != value.strip()
		or len(value) > 140
		or any(ord(character) < 32 for character in value)
	):
		raise ImmutableLedgerContractError(f"{label} is invalid")
	return value


def normalize_ledger_values(
	values: object,
	*,
	allowed_fields: frozenset[str] | None = None,
	definition: ImmutableLedgerDefinition | None = None,
) -> dict[str, object]:
	if not isinstance(values, Mapping):
		raise ImmutableLedgerContractError("ledger values must be an object")
	normalized: dict[str, object] = {}
	for fieldname, value in values.items():
		if (
			not isinstance(fieldname, str)
			or FIELDNAME_PATTERN.fullmatch(fieldname) is None
			or fieldname in RESERVED_LEDGER_FIELDS
		):
			raise ImmutableLedgerContractError("ledger values contain an invalid field")
		if allowed_fields is not None and fieldname not in allowed_fields:
			raise ImmutableLedgerContractError("ledger values contain an undeclared field")
		if isinstance(value, datetime | date | time):
			normalized[fieldname] = value.isoformat()
		elif isinstance(value, Decimal):
			normalized[fieldname] = format(value, "f")
		elif value is None or isinstance(value, bool | int | float | str | dict | list | tuple):
			normalized[fieldname] = value
		else:
			raise ImmutableLedgerContractError("ledger values contain an unsupported value")
	if "dimensions_json" in normalized:
		normalized["dimensions_json"] = normalize_dimensions_json(normalized["dimensions_json"])
	if "source_hash" in normalized:
		normalized["source_hash"] = normalize_optional_source_hash(normalized["source_hash"])
	if definition is not None and definition.amount_schema_version == 2:
		normalized.update(normalize_exact_ledger_money(normalized))
	return normalized


def _as_decimal(value: object, *, fieldname: str) -> Decimal:
	if isinstance(value, bool) or not isinstance(value, int | float | Decimal | str):
		raise ImmutableLedgerContractError(f"{fieldname} must be numeric")
	try:
		decimal = Decimal(str(value))
	except InvalidOperation as exc:
		raise ImmutableLedgerContractError(f"{fieldname} must be numeric") from exc
	if not decimal.is_finite():
		raise ImmutableLedgerContractError(f"{fieldname} must be finite")
	return decimal


def _negate(value: object, *, fieldname: str, exact=False) -> object:
	if value in (None, ""):
		return value
	decimal = _as_decimal(value, fieldname=fieldname)
	decimal = decimal.copy_negate() if exact else -decimal
	if isinstance(value, int) and not isinstance(value, bool):
		return int(decimal)
	if isinstance(value, float):
		return float(decimal)
	if isinstance(value, Decimal):
		return decimal
	return format(decimal, "f")


def _numeric_equal(first: object, second: object, *, fieldname: str) -> bool:
	if first in (None, "") and second in (None, ""):
		return True
	return _as_decimal(first, fieldname=fieldname) == _as_decimal(second, fieldname=fieldname)


def build_reversal_values(
	original: Mapping[str, object],
	*,
	definition: ImmutableLedgerDefinition,
	fieldnames: frozenset[str],
	overrides: object,
) -> dict[str, object]:
	original_name = normalize_ledger_name(original.get("name"), label="original entry name")
	if definition.amount_schema_version == 2:
		original = {**original, **normalize_exact_ledger_money(original)}
	normalized_overrides = normalize_ledger_values(
		overrides,
		allowed_fields=definition.reversal_override_fields,
	)
	for fieldname in definition.required_reversal_override_fields:
		if normalized_overrides.get(fieldname) in (None, ""):
			raise ImmutableLedgerContractError(f"required reversal override is missing: {fieldname}")
	missing_transforms = definition.transformed_fields - fieldnames
	if missing_transforms:
		raise ImmutableLedgerContractError(
			"ledger schema is missing reversal fields: " + ", ".join(sorted(missing_transforms))
		)

	values = {
		fieldname: original.get(fieldname)
		for fieldname in fieldnames
		if fieldname not in RESERVED_LEDGER_FIELDS
	}
	for fieldname in definition.negated_fields:
		values[fieldname] = _negate(
			original.get(fieldname),
			fieldname=fieldname,
			exact=definition.amount_schema_version == 2 and fieldname in EXACT_LEDGER_AMOUNT_FIELDS,
		)
	for first, second in definition.swapped_field_pairs:
		values[first] = original.get(second)
		values[second] = original.get(first)
	values.update(normalized_overrides)
	values["is_reversal"] = 1
	values["reversal_of"] = original_name
	if "dimensions_json" in values:
		values["dimensions_json"] = normalize_dimensions_json(values["dimensions_json"])
	if "source_hash" in values:
		values["source_hash"] = normalize_optional_source_hash(values["source_hash"])
	if definition.amount_schema_version == 2:
		values.update(normalize_exact_ledger_money(values))
	return values


def assert_reversal_matches(
	original: Mapping[str, object],
	reversal: Mapping[str, object],
	*,
	definition: ImmutableLedgerDefinition,
) -> None:
	original_name = normalize_ledger_name(original.get("name"), label="original entry name")
	if definition.amount_schema_version == 2:
		original = {**original, **normalize_exact_ledger_money(original)}
		reversal = {**reversal, **normalize_exact_ledger_money(reversal)}
		if original["currency_precision"] != reversal["currency_precision"]:
			raise ImmutableLedgerContractError("reversal changed captured currency precision")
	if not bool(reversal.get("is_reversal")) or reversal.get("reversal_of") != original_name:
		raise ImmutableLedgerContractError("reversal linkage is invalid")
	for fieldname in definition.negated_fields:
		expected = _negate(
			original.get(fieldname),
			fieldname=fieldname,
			exact=definition.amount_schema_version == 2 and fieldname in EXACT_LEDGER_AMOUNT_FIELDS,
		)
		if not _numeric_equal(reversal.get(fieldname), expected, fieldname=fieldname):
			raise ImmutableLedgerContractError(f"reversal value is not equal and opposite: {fieldname}")
	for first, second in definition.swapped_field_pairs:
		if not _numeric_equal(reversal.get(first), original.get(second), fieldname=first):
			raise ImmutableLedgerContractError(f"reversal swapped value is invalid: {first}")
		if not _numeric_equal(reversal.get(second), original.get(first), fieldname=second):
			raise ImmutableLedgerContractError(f"reversal swapped value is invalid: {second}")
	if definition.amount_schema_version == 2:
		excluded = (
			RESERVED_LEDGER_FIELDS
			| definition.transformed_fields
			| definition.reversal_override_fields
			| {"is_reversal", "reversal_of"}
		)
		for key, value in original.items():
			if key in excluded:
				continue
			left = normalize_ledger_values({key: value})
			right = normalize_ledger_values({key: reversal.get(key)})
			if left != right:
				raise ImmutableLedgerContractError("reversal changed original context")


def get_immutable_ledger_public_contract() -> ImmutableLedgerPublicContract:
	return {
		"schema_version": IMMUTABLE_LEDGER_SCHEMA_VERSION,
		"base_fields": [field.as_public_dict() for field in BASE_LEDGER_FIELDS],
		"mutation_policy": {
			"append_only": True,
			"update": False,
			"delete": False,
			"rename": False,
			"cancel": False,
			"direct_insert": False,
		},
		"reversal_policy": {
			"equal_and_opposite": True,
			"requires_reversal_of": True,
			"one_reversal_per_entry": True,
			"row_lock": True,
		},
		"http_write_enabled": False,
	}


__all__ = [
	"BASE_LEDGER_FIELDS",
	"BASE_LEDGER_FIELDS_BY_NAME",
	"IMMUTABLE_LEDGER_SCHEMA_VERSION",
	"ImmutableLedgerContractError",
	"ImmutableLedgerDefinition",
	"ImmutableLedgerPublicContract",
	"LedgerFieldContract",
	"assert_reversal_matches",
	"build_reversal_values",
	"get_immutable_ledger_public_contract",
	"normalize_dimensions_json",
	"normalize_ledger_name",
	"normalize_ledger_values",
	"normalize_optional_source_hash",
]
