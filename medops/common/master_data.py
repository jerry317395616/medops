from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal, cast

from medops.common.domain_service import fingerprint_json
from medops.common.organization import (
	normalize_boolean,
	normalize_code,
	normalize_nonnegative_integer,
	normalize_optional_text,
	normalize_positive_integer,
	normalize_reference,
	normalize_required_date,
	normalize_required_text,
)

MASTER_DATA_SCHEMA_VERSION = 1
MASTER_DATA_DOMAIN_DOCTYPE = "HRP Master Data Domain"
MASTER_DATA_REQUEST_DOCTYPE = "HRP Master Data Request"
MASTER_DATA_CHANGE_ITEM_DOCTYPE = "HRP Master Data Change Item"
MAX_CHANGE_ITEMS = 64
MAX_CHANGE_PAYLOAD_BYTES = 64 * 1024
MAX_VALUE_LENGTH = 500
FIELD_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SENSITIVE_FIELD_TOKENS = frozenset(
	{
		"access_token",
		"api_key",
		"api_secret",
		"authorization",
		"password",
		"private_key",
		"secret",
		"token",
	}
)
REQUEST_OPERATIONS = ("Create", "Update", "Disable")
REVIEW_DECISIONS = ("Approve", "Reject")
MasterDataOperation = Literal["Create", "Update", "Disable"]
MasterDataReviewDecision = Literal["Approve", "Reject"]
MasterDataValueType = Literal["Text", "Boolean", "Link", "Choice"]


class MasterDataContractError(ValueError):
	"""Raised when a master-data command violates the public contract."""


@dataclass(frozen=True, slots=True)
class MasterDataFieldPolicy:
	field_name: str
	label: str
	value_type: MasterDataValueType
	required_on_create: bool = False
	link_doctype: str | None = None
	choices: tuple[str, ...] = ()
	maximum: int = 140

	def as_public_dict(self) -> dict[str, object]:
		result: dict[str, object] = {
			"field_name": self.field_name,
			"label": self.label,
			"value_type": self.value_type,
			"required_on_create": self.required_on_create,
		}
		if self.link_doctype:
			result["link_doctype"] = self.link_doctype
		if self.choices:
			result["choices"] = list(self.choices)
		return result


@dataclass(frozen=True, slots=True)
class MasterDataTargetPolicy:
	target_doctype: str
	company_field: str | None
	fields: tuple[MasterDataFieldPolicy, ...]
	allow_create: bool = True
	allow_update: bool = True
	allow_disable: bool = True
	version: int = 1

	@property
	def fields_by_name(self) -> dict[str, MasterDataFieldPolicy]:
		return {field.field_name: field for field in self.fields}

	@property
	def digest(self) -> str:
		return fingerprint_json(self.as_public_dict())

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": MASTER_DATA_SCHEMA_VERSION,
			"target_doctype": self.target_doctype,
			"company_field": self.company_field,
			"allow_create": self.allow_create,
			"allow_update": self.allow_update,
			"allow_disable": self.allow_disable,
			"version": self.version,
			"fields": [field.as_public_dict() for field in self.fields],
		}


def _text(
	field_name: str,
	label: str,
	*,
	required: bool = False,
	maximum: int = 140,
) -> MasterDataFieldPolicy:
	return MasterDataFieldPolicy(
		field_name=field_name,
		label=label,
		value_type="Text",
		required_on_create=required,
		maximum=maximum,
	)


def _boolean(field_name: str, label: str) -> MasterDataFieldPolicy:
	return MasterDataFieldPolicy(
		field_name=field_name,
		label=label,
		value_type="Boolean",
	)


def _link(
	field_name: str,
	label: str,
	link_doctype: str,
	*,
	required: bool = False,
) -> MasterDataFieldPolicy:
	return MasterDataFieldPolicy(
		field_name=field_name,
		label=label,
		value_type="Link",
		required_on_create=required,
		link_doctype=link_doctype,
	)


def _choice(
	field_name: str,
	label: str,
	choices: tuple[str, ...],
	*,
	required: bool = False,
) -> MasterDataFieldPolicy:
	return MasterDataFieldPolicy(
		field_name=field_name,
		label=label,
		value_type="Choice",
		required_on_create=required,
		choices=choices,
	)


MASTER_DATA_TARGET_POLICIES = {
	policy.target_doctype: policy
	for policy in (
		MasterDataTargetPolicy(
			target_doctype="Department",
			company_field="company",
			fields=(
				_text("department_name", "部门名称", required=True),
				_link("parent_department", "上级部门", "Department"),
				_boolean("is_group", "分组"),
				_boolean("disabled", "停用"),
			),
		),
		MasterDataTargetPolicy(
			target_doctype="Cost Center",
			company_field="company",
			fields=(
				_text("cost_center_name", "成本中心名称", required=True),
				_link("parent_cost_center", "上级成本中心", "Cost Center"),
				_boolean("is_group", "分组"),
				_boolean("disabled", "停用"),
			),
		),
		MasterDataTargetPolicy(
			target_doctype="Item",
			company_field=None,
			fields=(
				_text("item_name", "物料名称", required=True),
				_link("item_group", "物料组", "Item Group", required=True),
				_link("stock_uom", "库存单位", "UOM", required=True),
				_boolean("is_stock_item", "库存物料"),
				_boolean("disabled", "停用"),
			),
		),
		MasterDataTargetPolicy(
			target_doctype="Supplier",
			company_field=None,
			fields=(
				_text("supplier_name", "供应商名称", required=True),
				_link("supplier_group", "供应商组", "Supplier Group", required=True),
				_choice(
					"supplier_type",
					"供应商类型",
					("Company", "Individual", "Partnership"),
					required=True,
				),
				_boolean("disabled", "停用"),
			),
		),
		MasterDataTargetPolicy(
			target_doctype="Warehouse",
			company_field="company",
			fields=(
				_text("warehouse_name", "仓库名称", required=True),
				_link("parent_warehouse", "上级仓库", "Warehouse"),
				_boolean("is_group", "分组"),
				_boolean("disabled", "停用"),
			),
		),
	)
}


def get_target_policy(target_doctype: object) -> MasterDataTargetPolicy:
	try:
		normalized = normalize_reference(target_doctype, label="target_doctype")
	except ValueError as exc:
		raise MasterDataContractError("target_doctype is invalid") from exc
	policy = MASTER_DATA_TARGET_POLICIES.get(normalized)
	if policy is None:
		raise MasterDataContractError("target_doctype is not supported")
	return policy


@dataclass(frozen=True, slots=True)
class MasterDataDomainUpsert:
	code: str
	display_name: str
	target_doctype: str
	enabled: bool
	expected_revision: int
	remarks: str | None

	def as_request_payload(self) -> dict[str, object]:
		return {
			"code": self.code,
			"display_name": self.display_name,
			"target_doctype": self.target_doctype,
			"enabled": self.enabled,
			"expected_revision": self.expected_revision,
			"remarks": self.remarks,
		}


def build_master_data_domain_upsert(
	*,
	code: object,
	display_name: object,
	target_doctype: object,
	enabled: object = True,
	expected_revision: object = 0,
	remarks: object = None,
) -> MasterDataDomainUpsert:
	policy = get_target_policy(target_doctype)
	try:
		return MasterDataDomainUpsert(
			code=normalize_code(code, label="code"),
			display_name=normalize_required_text(display_name, label="display_name"),
			target_doctype=policy.target_doctype,
			enabled=normalize_boolean(enabled, label="enabled"),
			expected_revision=normalize_nonnegative_integer(
				expected_revision,
				label="expected_revision",
			),
			remarks=normalize_optional_text(remarks, label="remarks"),
		)
	except ValueError as exc:
		raise MasterDataContractError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class ProposedMasterDataChange:
	field_name: str
	proposed_value: str
	reason: str | None

	def as_request_payload(self) -> dict[str, object]:
		return {
			"field_name": self.field_name,
			"proposed_value": self.proposed_value,
			"reason": self.reason,
		}


def _load_changes(value: object) -> list[object]:
	loaded = value
	if isinstance(value, str):
		if len(value.encode("utf-8")) > MAX_CHANGE_PAYLOAD_BYTES:
			raise MasterDataContractError("changes payload is too large")
		try:
			loaded = json.loads(value)
		except json.JSONDecodeError as exc:
			raise MasterDataContractError("changes must contain valid JSON") from exc
	if not isinstance(loaded, list):
		raise MasterDataContractError("changes must be a list")
	if not loaded or len(loaded) > MAX_CHANGE_ITEMS:
		raise MasterDataContractError("changes count is outside the allowed range")
	if not isinstance(value, str):
		try:
			size = len(
				json.dumps(
					loaded,
					ensure_ascii=False,
					separators=(",", ":"),
				).encode("utf-8")
			)
		except (TypeError, ValueError) as exc:
			raise MasterDataContractError("changes must contain JSON-compatible values") from exc
		if size > MAX_CHANGE_PAYLOAD_BYTES:
			raise MasterDataContractError("changes payload is too large")
	return loaded


def _normalize_proposed_scalar(value: object) -> str:
	if isinstance(value, bool):
		return "1" if value else "0"
	if isinstance(value, int | float) and not isinstance(value, bool):
		return str(value)
	if not isinstance(value, str):
		raise MasterDataContractError("proposed_value must be scalar")
	normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
	if len(normalized) > MAX_VALUE_LENGTH or any(
		ord(character) < 32 and character not in {"\n", "\t"} for character in normalized
	):
		raise MasterDataContractError("proposed_value is invalid")
	return normalized


def normalize_proposed_changes(value: object) -> tuple[ProposedMasterDataChange, ...]:
	result: list[ProposedMasterDataChange] = []
	seen: set[str] = set()
	for item in _load_changes(value):
		if not isinstance(item, dict):
			raise MasterDataContractError("each change must be an object")
		if set(item) - {"field_name", "proposed_value", "reason"}:
			raise MasterDataContractError("change contains unsupported fields")
		if "field_name" not in item or "proposed_value" not in item:
			raise MasterDataContractError("change is missing a required field")
		field_name = item["field_name"]
		if (
			not isinstance(field_name, str)
			or FIELD_NAME_PATTERN.fullmatch(field_name) is None
			or field_name in SENSITIVE_FIELD_TOKENS
			or any(token in field_name for token in ("password", "secret", "token"))
		):
			raise MasterDataContractError("field_name is invalid")
		if field_name in seen:
			raise MasterDataContractError("change field names must be unique")
		seen.add(field_name)
		try:
			reason = normalize_optional_text(item.get("reason"), label="reason")
		except ValueError as exc:
			raise MasterDataContractError(str(exc)) from exc
		result.append(
			ProposedMasterDataChange(
				field_name=field_name,
				proposed_value=_normalize_proposed_scalar(item["proposed_value"]),
				reason=reason,
			)
		)
	return tuple(result)


def normalize_policy_value(
	policy: MasterDataFieldPolicy,
	value: object,
	*,
	allow_empty: bool,
) -> str:
	if policy.value_type == "Boolean":
		try:
			return "1" if normalize_boolean(value, label=policy.field_name) else "0"
		except ValueError as exc:
			raise MasterDataContractError(str(exc)) from exc
	normalized = _normalize_proposed_scalar(value)
	if not normalized:
		if allow_empty:
			return ""
		raise MasterDataContractError(f"{policy.field_name} is required")
	if policy.value_type == "Text" and len(normalized) > policy.maximum:
		raise MasterDataContractError(f"{policy.field_name} is too long")
	if policy.value_type == "Choice" and normalized not in policy.choices:
		raise MasterDataContractError(f"{policy.field_name} has an unsupported value")
	return normalized


@dataclass(frozen=True, slots=True)
class MasterDataRequestUpsert:
	request_name: str | None
	master_data_domain: str
	company: str
	hospital: str
	organization_unit: str
	operation: MasterDataOperation
	target_name: str | None
	subject: str
	effective_on: str
	changes: tuple[ProposedMasterDataChange, ...]
	expected_revision: int

	def as_request_payload(self) -> dict[str, object]:
		return {
			"request_name": self.request_name,
			"master_data_domain": self.master_data_domain,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit,
			"operation": self.operation,
			"target_name": self.target_name,
			"subject": self.subject,
			"effective_on": self.effective_on,
			"changes": [change.as_request_payload() for change in self.changes],
			"expected_revision": self.expected_revision,
		}


def build_master_data_request_upsert(
	*,
	master_data_domain: object,
	company: object,
	hospital: object,
	organization_unit: object,
	operation: object,
	subject: object,
	effective_on: object,
	changes: object,
	expected_revision: object = 0,
	request_name: object = None,
	target_name: object = None,
) -> MasterDataRequestUpsert:
	if not isinstance(operation, str) or operation not in REQUEST_OPERATIONS:
		raise MasterDataContractError("operation is invalid")
	try:
		normalized_request = (
			None if request_name in (None, "") else normalize_reference(request_name, label="request_name")
		)
		normalized_target = (
			None if target_name in (None, "") else normalize_reference(target_name, label="target_name")
		)
		revision = normalize_nonnegative_integer(
			expected_revision,
			label="expected_revision",
		)
		if bool(normalized_request) != (revision > 0):
			raise MasterDataContractError("request_name and expected_revision conflict")
		if operation == "Create" and normalized_target is not None:
			raise MasterDataContractError("create request cannot specify target_name")
		if operation != "Create" and normalized_target is None:
			raise MasterDataContractError("target_name is required")
		return MasterDataRequestUpsert(
			request_name=normalized_request,
			master_data_domain=normalize_code(
				master_data_domain,
				label="master_data_domain",
			),
			company=normalize_reference(company, label="company"),
			hospital=normalize_code(hospital, label="hospital"),
			organization_unit=normalize_reference(
				organization_unit,
				label="organization_unit",
			),
			operation=cast(MasterDataOperation, operation),
			target_name=normalized_target,
			subject=normalize_required_text(subject, label="subject"),
			effective_on=normalize_required_date(
				effective_on,
				label="effective_on",
			),
			changes=normalize_proposed_changes(changes),
			expected_revision=revision,
		)
	except ValueError as exc:
		if isinstance(exc, MasterDataContractError):
			raise
		raise MasterDataContractError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class MasterDataRequestSubmit:
	request_name: str
	expected_revision: int

	def as_request_payload(self) -> dict[str, object]:
		return {
			"request_name": self.request_name,
			"expected_revision": self.expected_revision,
		}


def build_master_data_request_submit(
	*,
	request_name: object,
	expected_revision: object,
) -> MasterDataRequestSubmit:
	try:
		return MasterDataRequestSubmit(
			request_name=normalize_reference(request_name, label="request_name"),
			expected_revision=normalize_positive_integer(
				expected_revision,
				label="expected_revision",
			),
		)
	except ValueError as exc:
		raise MasterDataContractError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class MasterDataRequestReview:
	request_name: str
	expected_revision: int
	decision: MasterDataReviewDecision
	reason: str | None

	def as_request_payload(self) -> dict[str, object]:
		return {
			"request_name": self.request_name,
			"expected_revision": self.expected_revision,
			"decision": self.decision,
			"reason": self.reason,
		}


def build_master_data_request_review(
	*,
	request_name: object,
	expected_revision: object,
	decision: object,
	reason: object = None,
) -> MasterDataRequestReview:
	if not isinstance(decision, str) or decision not in REVIEW_DECISIONS:
		raise MasterDataContractError("decision is invalid")
	try:
		normalized_reason = normalize_optional_text(reason, label="reason")
		if decision == "Reject" and normalized_reason is None:
			raise MasterDataContractError("rejection reason is required")
		return MasterDataRequestReview(
			request_name=normalize_reference(request_name, label="request_name"),
			expected_revision=normalize_positive_integer(
				expected_revision,
				label="expected_revision",
			),
			decision=cast(MasterDataReviewDecision, decision),
			reason=normalized_reason,
		)
	except ValueError as exc:
		if isinstance(exc, MasterDataContractError):
			raise
		raise MasterDataContractError(str(exc)) from exc


def serialize_stored_value(value: object) -> str:
	if value in (None, ""):
		return ""
	if isinstance(value, bool):
		return "1" if value else "0"
	if isinstance(value, date):
		return value.isoformat()
	return str(value)


__all__ = [
	"MASTER_DATA_CHANGE_ITEM_DOCTYPE",
	"MASTER_DATA_DOMAIN_DOCTYPE",
	"MASTER_DATA_REQUEST_DOCTYPE",
	"MASTER_DATA_SCHEMA_VERSION",
	"MASTER_DATA_TARGET_POLICIES",
	"MasterDataContractError",
	"MasterDataDomainUpsert",
	"MasterDataFieldPolicy",
	"MasterDataOperation",
	"MasterDataRequestReview",
	"MasterDataRequestSubmit",
	"MasterDataRequestUpsert",
	"MasterDataReviewDecision",
	"MasterDataTargetPolicy",
	"ProposedMasterDataChange",
	"build_master_data_domain_upsert",
	"build_master_data_request_review",
	"build_master_data_request_submit",
	"build_master_data_request_upsert",
	"get_target_policy",
	"normalize_policy_value",
	"normalize_proposed_changes",
	"serialize_stored_value",
]
