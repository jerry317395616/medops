from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

from medops.common.domain_service import fingerprint_json
from medops.common.organization import (
	OrganizationContractError,
	normalize_boolean,
	normalize_code,
	normalize_optional_date,
	normalize_optional_text,
	normalize_positive_integer,
	normalize_reference,
	normalize_required_text,
	validate_date_range,
)

ACCESS_SCOPE_SCHEMA_VERSION = 1
ACCESS_SCOPE_DOCTYPE = "HRP Access Scope"
ACCESS_SCOPE_MEMBER_DOCTYPE = "HRP Access Scope Member"
ACCESS_SCOPE_LEVELS = ("Company", "Hospital", "Organization Unit")
ACCESS_ACTIONS = ("read", "create", "write", "submit", "cancel", "export")
MAX_SCOPE_MEMBERS = 500

AccessScopeLevel = Literal["Company", "Hospital", "Organization Unit"]
AccessAction = Literal["read", "create", "write", "submit", "cancel", "export"]


class ScopeFilterCondition(TypedDict):
	field: str
	operator: str
	value: object


class ScopeFilterGroup(TypedDict):
	operator: str
	conditions: list[ScopeFilterCondition]


class ScopeFilters(TypedDict):
	operator: str
	groups: list[ScopeFilterGroup]
	fail_closed: bool


class ScopeMasking(TypedDict):
	policy: str
	fields: list[str]


class ScopeDecision(TypedDict):
	schema_version: int
	user: str
	doctype: str
	action: str
	allowed: bool
	unrestricted: bool
	base_permission: bool
	scopes: list[dict[str, object]]
	filters: ScopeFilters
	masking: ScopeMasking
	decision_digest: str


class AccessScopeContractError(ValueError):
	"""Raised when an access-scope definition or query violates the public contract."""


def normalize_action(value: object) -> AccessAction:
	if not isinstance(value, str):
		raise AccessScopeContractError("action must be text")
	normalized = value.strip().lower()
	if normalized not in ACCESS_ACTIONS:
		raise AccessScopeContractError("action is invalid")
	return normalized


def normalize_doctype(value: object) -> str:
	try:
		return normalize_required_text(value, label="doctype")
	except OrganizationContractError as exc:
		raise AccessScopeContractError(str(exc)) from exc


def scope_level_for(*, hospital: str | None, organization_unit: str | None) -> AccessScopeLevel:
	if organization_unit:
		if not hospital:
			raise AccessScopeContractError("organization_unit requires hospital")
		return "Organization Unit"
	if hospital:
		return "Hospital"
	return "Company"


@dataclass(frozen=True, slots=True)
class AccessScopeMember:
	user: str
	target_doctype: str | None
	allow_read: bool
	allow_create: bool
	allow_write: bool
	allow_submit: bool
	allow_cancel: bool
	allow_export: bool
	enabled: bool

	def allows(self, action: AccessAction) -> bool:
		return bool(getattr(self, f"allow_{action}")) and self.enabled

	def as_dict(self) -> dict[str, object]:
		return {
			"user": self.user,
			"target_doctype": self.target_doctype,
			"allow_read": self.allow_read,
			"allow_create": self.allow_create,
			"allow_write": self.allow_write,
			"allow_submit": self.allow_submit,
			"allow_cancel": self.allow_cancel,
			"allow_export": self.allow_export,
			"enabled": self.enabled,
		}


def build_access_scope_member(
	*,
	user: object,
	target_doctype: object = None,
	allow_read: object = True,
	allow_create: object = False,
	allow_write: object = False,
	allow_submit: object = False,
	allow_cancel: object = False,
	allow_export: object = False,
	enabled: object = True,
) -> AccessScopeMember:
	try:
		member = AccessScopeMember(
			user=normalize_reference(user, label="user"),
			target_doctype=(normalize_doctype(target_doctype) if target_doctype else None),
			allow_read=normalize_boolean(allow_read, label="allow_read"),
			allow_create=normalize_boolean(allow_create, label="allow_create"),
			allow_write=normalize_boolean(allow_write, label="allow_write"),
			allow_submit=normalize_boolean(allow_submit, label="allow_submit"),
			allow_cancel=normalize_boolean(allow_cancel, label="allow_cancel"),
			allow_export=normalize_boolean(allow_export, label="allow_export"),
			enabled=normalize_boolean(enabled, label="enabled"),
		)
	except OrganizationContractError as exc:
		raise AccessScopeContractError(str(exc)) from exc
	if member.enabled and not any(bool(getattr(member, f"allow_{action}")) for action in ACCESS_ACTIONS):
		raise AccessScopeContractError("an enabled member must allow at least one action")
	return member


@dataclass(frozen=True, slots=True)
class AccessScopeDefinition:
	code: str
	display_name: str
	company: str
	hospital: str | None
	organization_unit: str | None
	include_descendants: bool
	enabled: bool
	valid_from: str | None
	valid_to: str | None
	revision: int
	remarks: str | None
	members: tuple[AccessScopeMember, ...]
	scope_level: AccessScopeLevel
	policy_digest: str

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": ACCESS_SCOPE_SCHEMA_VERSION,
			"code": self.code,
			"display_name": self.display_name,
			"scope_level": self.scope_level,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit,
			"include_descendants": self.include_descendants,
			"enabled": self.enabled,
			"valid_from": self.valid_from,
			"valid_to": self.valid_to,
			"revision": self.revision,
			"remarks": self.remarks,
			"policy_digest": self.policy_digest,
		}


def build_access_scope_definition(
	*,
	code: object,
	display_name: object,
	company: object,
	hospital: object = None,
	organization_unit: object = None,
	include_descendants: object = False,
	enabled: object = True,
	valid_from: object = None,
	valid_to: object = None,
	revision: object = 1,
	remarks: object = None,
	members: tuple[AccessScopeMember, ...] | list[AccessScopeMember],
) -> AccessScopeDefinition:
	if not members or len(members) > MAX_SCOPE_MEMBERS:
		raise AccessScopeContractError("members count is outside the allowed range")
	try:
		normalized_company = normalize_reference(company, label="company")
		normalized_hospital = normalize_code(hospital, label="hospital") if hospital else None
		normalized_unit = (
			normalize_reference(organization_unit, label="organization_unit") if organization_unit else None
		)
		start = normalize_optional_date(valid_from, label="valid_from")
		end = normalize_optional_date(valid_to, label="valid_to")
		validate_date_range(start, end)
		level = scope_level_for(hospital=normalized_hospital, organization_unit=normalized_unit)
		descendants = normalize_boolean(include_descendants, label="include_descendants")
		if descendants and level != "Organization Unit":
			raise AccessScopeContractError("include_descendants requires organization_unit")
		normalized_members = tuple(members)
		member_keys = {(member.user, member.target_doctype) for member in normalized_members}
		if len(member_keys) != len(normalized_members):
			raise AccessScopeContractError("user and target_doctype must be unique within a scope")
		normalized_code = normalize_code(code, label="code")
		normalized_name = normalize_required_text(display_name, label="display_name")
		normalized_enabled = normalize_boolean(enabled, label="enabled")
		normalized_revision = normalize_positive_integer(revision, label="revision")
		normalized_remarks = normalize_optional_text(remarks, label="remarks")
		policy_payload = {
			"schema_version": ACCESS_SCOPE_SCHEMA_VERSION,
			"code": normalized_code,
			"display_name": normalized_name,
			"company": normalized_company,
			"hospital": normalized_hospital,
			"organization_unit": normalized_unit,
			"include_descendants": descendants,
			"enabled": normalized_enabled,
			"valid_from": start,
			"valid_to": end,
			"remarks": normalized_remarks,
			"members": [
				member.as_dict()
				for member in sorted(
					normalized_members,
					key=lambda item: (item.user, item.target_doctype or ""),
				)
			],
		}
	except OrganizationContractError as exc:
		raise AccessScopeContractError(str(exc)) from exc
	return AccessScopeDefinition(
		code=normalized_code,
		display_name=normalized_name,
		company=normalized_company,
		hospital=normalized_hospital,
		organization_unit=normalized_unit,
		include_descendants=descendants,
		enabled=normalized_enabled,
		valid_from=start,
		valid_to=end,
		revision=normalized_revision,
		remarks=normalized_remarks,
		members=normalized_members,
		scope_level=level,
		policy_digest=fingerprint_json(policy_payload),
	)


@dataclass(frozen=True, slots=True)
class ScopeGrant:
	code: str
	company: str
	hospital: str | None
	organization_unit: str | None
	include_descendants: bool
	organization_units: tuple[str, ...]


def _condition(field: str, operator: str, value: object) -> ScopeFilterCondition:
	return {"field": field, "operator": operator, "value": value}


def filter_group_for(
	grant: ScopeGrant,
	*,
	dimension_fields: dict[str, str],
) -> ScopeFilterGroup | None:
	company_field = dimension_fields.get("company")
	if not company_field:
		return None
	conditions = [_condition(company_field, "=", grant.company)]
	if grant.hospital:
		hospital_field = dimension_fields.get("hospital")
		if not hospital_field:
			return None
		conditions.append(_condition(hospital_field, "=", grant.hospital))
	if grant.organization_unit:
		organization_field = dimension_fields.get("organization_unit")
		if not organization_field:
			return None
		units = grant.organization_units or (grant.organization_unit,)
		operator = "=" if len(units) == 1 else "in"
		value: object = units[0] if len(units) == 1 else list(units)
		conditions.append(_condition(organization_field, operator, value))
	return {"operator": "AND", "conditions": conditions}


def build_scope_decision(
	*,
	user: str,
	doctype: str,
	action: AccessAction,
	dimension_fields: dict[str, str],
	grants: tuple[ScopeGrant, ...],
	base_permission: bool,
	unrestricted: bool = False,
) -> ScopeDecision:
	groups: list[ScopeFilterGroup] = []
	resolved_scopes: list[dict[str, object]] = []
	for grant in grants:
		group = filter_group_for(grant, dimension_fields=dimension_fields)
		if group is None:
			continue
		groups.append(group)
		resolved_scopes.append(
			{
				"code": grant.code,
				"company": grant.company,
				"hospital": grant.hospital,
				"organization_unit": grant.organization_unit,
				"include_descendants": grant.include_descendants,
			}
		)
	allowed = bool(base_permission and (unrestricted or groups))
	result: ScopeDecision = {
		"schema_version": ACCESS_SCOPE_SCHEMA_VERSION,
		"user": user,
		"doctype": doctype,
		"action": action,
		"allowed": allowed,
		"unrestricted": bool(unrestricted and base_permission),
		"base_permission": base_permission,
		"scopes": resolved_scopes,
		"filters": {
			"operator": "OR",
			"groups": [] if unrestricted else groups,
			"fail_closed": not allowed,
		},
		"masking": {"policy": "none", "fields": []},
		"decision_digest": "",
	}
	result["decision_digest"] = fingerprint_json(result)
	return result


__all__ = [
	"ACCESS_ACTIONS",
	"ACCESS_SCOPE_DOCTYPE",
	"ACCESS_SCOPE_LEVELS",
	"ACCESS_SCOPE_MEMBER_DOCTYPE",
	"ACCESS_SCOPE_SCHEMA_VERSION",
	"MAX_SCOPE_MEMBERS",
	"AccessAction",
	"AccessScopeContractError",
	"AccessScopeDefinition",
	"AccessScopeMember",
	"ScopeDecision",
	"ScopeFilterCondition",
	"ScopeFilterGroup",
	"ScopeGrant",
	"build_access_scope_definition",
	"build_access_scope_member",
	"build_scope_decision",
	"filter_group_for",
	"normalize_action",
	"normalize_doctype",
	"scope_level_for",
]
