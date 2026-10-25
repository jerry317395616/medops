from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, TypedDict, cast

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

SEGREGATION_SCHEMA_VERSION = 1
SEGREGATION_RULE_DOCTYPE = "HRP Segregation Rule"
SEGREGATION_ACTIONS = (
	"amend",
	"approve",
	"cancel",
	"create",
	"pay",
	"post",
	"reconcile",
	"release",
	"review",
	"submit",
)
MAX_SEGREGATION_SOURCES = 16
MAX_SEGREGATION_JSON_BYTES = 16 * 1024
ACTOR_FIELD_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SegregationAction = Literal[
	"amend",
	"approve",
	"cancel",
	"create",
	"pay",
	"post",
	"reconcile",
	"release",
	"review",
	"submit",
]


class SegregationContractError(ValueError):
	"""Raised when a segregation policy or evaluation request is unsafe."""


class SegregationConflict(TypedDict, total=False):
	rule: str | None
	code: str
	revision: int
	policy_digest: str
	reason: str
	matched_actor_fields: list[str]
	matched_roles: list[str]


class SegregationDecision(TypedDict):
	schema_version: int
	action: SegregationAction
	allowed: bool
	rules_evaluated: int
	conflicts: list[SegregationConflict]
	decision_digest: str


def normalize_segregation_action(value: object) -> SegregationAction:
	if not isinstance(value, str):
		raise SegregationContractError("action is invalid")
	normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
	if normalized not in SEGREGATION_ACTIONS:
		raise SegregationContractError("action is invalid")
	return cast(SegregationAction, normalized)


def _load_bounded_list(value: object, *, label: str) -> list[object]:
	loaded = value
	if value in (None, ""):
		loaded = []
	elif isinstance(value, str):
		if len(value.encode("utf-8")) > MAX_SEGREGATION_JSON_BYTES:
			raise SegregationContractError(f"{label} payload is too large")
		try:
			loaded = json.loads(value)
		except json.JSONDecodeError as exc:
			raise SegregationContractError(f"{label} must contain valid JSON") from exc
	elif isinstance(value, tuple):
		loaded = list(value)
	if not isinstance(loaded, list) or len(loaded) > MAX_SEGREGATION_SOURCES:
		raise SegregationContractError(f"{label} must be a bounded list")
	try:
		if len(canonical_json(loaded).encode("utf-8")) > MAX_SEGREGATION_JSON_BYTES:
			raise SegregationContractError(f"{label} payload is too large")
	except ValueError as exc:
		raise SegregationContractError(f"{label} must contain JSON-compatible values") from exc
	return loaded


def build_actor_fields(value: object) -> tuple[str, ...]:
	fields: list[str] = []
	for raw in _load_bounded_list(value, label="actor_fields"):
		if not isinstance(raw, str) or raw != raw.strip() or ACTOR_FIELD_PATTERN.fullmatch(raw) is None:
			raise SegregationContractError("actor_fields contains an invalid field")
		fields.append(raw)
	if len(set(fields)) != len(fields):
		raise SegregationContractError("actor_fields contains duplicates")
	return tuple(sorted(fields))


def build_conflicting_roles(value: object) -> tuple[str, ...]:
	try:
		roles = [
			normalize_reference(raw, label="conflicting_role")
			for raw in _load_bounded_list(value, label="conflicting_roles")
		]
	except OrganizationContractError as exc:
		raise SegregationContractError(str(exc)) from exc
	if len(set(roles)) != len(roles):
		raise SegregationContractError("conflicting_roles contains duplicates")
	return tuple(sorted(roles))


@dataclass(frozen=True, slots=True)
class SegregationRuleDefinition:
	code: str
	display_name: str
	target_doctype: str
	target_action: SegregationAction
	company: str
	hospital: str
	organization_unit: str | None
	include_descendants: bool
	actor_fields: tuple[str, ...]
	conflicting_roles: tuple[str, ...]
	enabled: bool
	valid_from: str
	valid_to: str | None
	revision: int
	remarks: str | None
	policy_digest: str

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": SEGREGATION_SCHEMA_VERSION,
			"code": self.code,
			"display_name": self.display_name,
			"target_doctype": self.target_doctype,
			"target_action": self.target_action,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit,
			"include_descendants": self.include_descendants,
			"actor_fields": list(self.actor_fields),
			"conflicting_roles": list(self.conflicting_roles),
			"enabled": self.enabled,
			"valid_from": self.valid_from,
			"valid_to": self.valid_to,
			"revision": self.revision,
			"remarks": self.remarks,
			"policy_digest": self.policy_digest,
		}


def build_segregation_rule_definition(
	*,
	code: object,
	display_name: object,
	target_doctype: object,
	target_action: object,
	company: object,
	hospital: object,
	valid_from: object,
	actor_fields: object = None,
	conflicting_roles: object = None,
	organization_unit: object = None,
	include_descendants: object = False,
	enabled: object = True,
	valid_to: object = None,
	revision: object = 1,
	remarks: object = None,
) -> SegregationRuleDefinition:
	try:
		start = normalize_required_date(valid_from, label="valid_from")
		end = normalize_optional_date(valid_to, label="valid_to")
		validate_date_range(start, end)
		unit = (
			normalize_reference(organization_unit, label="organization_unit") if organization_unit else None
		)
		descendants = normalize_boolean(include_descendants, label="include_descendants")
		if descendants and unit is None:
			raise SegregationContractError("include_descendants requires organization_unit")
		normalized_fields = build_actor_fields(actor_fields)
		normalized_roles = build_conflicting_roles(conflicting_roles)
		if not normalized_fields and not normalized_roles:
			raise SegregationContractError("a rule requires actor_fields or conflicting_roles")
		payload: dict[str, object] = {
			"schema_version": SEGREGATION_SCHEMA_VERSION,
			"code": normalize_code(code, label="code"),
			"display_name": normalize_required_text(display_name, label="display_name"),
			"target_doctype": normalize_reference(target_doctype, label="target_doctype"),
			"target_action": normalize_segregation_action(target_action),
			"company": normalize_reference(company, label="company"),
			"hospital": normalize_reference(hospital, label="hospital"),
			"organization_unit": unit,
			"include_descendants": descendants,
			"actor_fields": list(normalized_fields),
			"conflicting_roles": list(normalized_roles),
			"enabled": normalize_boolean(enabled, label="enabled"),
			"valid_from": start,
			"valid_to": end,
			"remarks": normalize_optional_text(remarks, label="remarks"),
		}
		revision_value = normalize_positive_integer(revision, label="revision")
	except OrganizationContractError as exc:
		raise SegregationContractError(str(exc)) from exc
	return SegregationRuleDefinition(
		code=str(payload["code"]),
		display_name=str(payload["display_name"]),
		target_doctype=str(payload["target_doctype"]),
		target_action=payload["target_action"],  # type: ignore[arg-type]
		company=str(payload["company"]),
		hospital=str(payload["hospital"]),
		organization_unit=unit,
		include_descendants=descendants,
		actor_fields=normalized_fields,
		conflicting_roles=normalized_roles,
		enabled=bool(payload["enabled"]),
		valid_from=start,
		valid_to=end,
		revision=revision_value,
		remarks=payload["remarks"] if isinstance(payload["remarks"], str) else None,
		policy_digest=fingerprint_json(payload),
	)


@dataclass(frozen=True, slots=True)
class SegregationRuleUpsert:
	rule_name: str | None
	definition: SegregationRuleDefinition
	expected_revision: int

	def as_request_payload(self) -> dict[str, object]:
		return {
			"rule_name": self.rule_name,
			"expected_revision": self.expected_revision,
			**self.definition.as_public_dict(),
		}


def build_segregation_rule_upsert(
	*,
	code: object,
	display_name: object,
	target_doctype: object,
	target_action: object,
	company: object,
	hospital: object,
	valid_from: object,
	actor_fields: object = None,
	conflicting_roles: object = None,
	organization_unit: object = None,
	include_descendants: object = False,
	enabled: object = True,
	valid_to: object = None,
	remarks: object = None,
	rule_name: object = None,
	expected_revision: object = 0,
) -> SegregationRuleUpsert:
	try:
		expected = normalize_nonnegative_integer(expected_revision, label="expected_revision")
		name = normalize_reference(rule_name, label="rule_name") if rule_name else None
	except OrganizationContractError as exc:
		raise SegregationContractError(str(exc)) from exc
	return SegregationRuleUpsert(
		rule_name=name,
		expected_revision=expected,
		definition=build_segregation_rule_definition(
			code=code,
			display_name=display_name,
			target_doctype=target_doctype,
			target_action=target_action,
			company=company,
			hospital=hospital,
			organization_unit=organization_unit,
			include_descendants=include_descendants,
			actor_fields=actor_fields,
			conflicting_roles=conflicting_roles,
			enabled=enabled,
			valid_from=valid_from,
			valid_to=valid_to,
			revision=max(expected, 1),
			remarks=remarks,
		),
	)


@dataclass(frozen=True, slots=True)
class SegregationEvaluation:
	user: str
	action: SegregationAction
	target_doctype: str
	docname: str

	def as_request_payload(self) -> dict[str, object]:
		return {
			"user": self.user,
			"action": self.action,
			"doctype": self.target_doctype,
			"docname": self.docname,
		}


def build_segregation_evaluation(
	*,
	user: object,
	action: object,
	doctype: object,
	docname: object,
) -> SegregationEvaluation:
	try:
		return SegregationEvaluation(
			user=normalize_reference(user, label="user"),
			action=normalize_segregation_action(action),
			target_doctype=normalize_reference(doctype, label="doctype"),
			docname=normalize_reference(docname, label="docname"),
		)
	except OrganizationContractError as exc:
		raise SegregationContractError(str(exc)) from exc


def build_segregation_decision(
	*,
	evaluation: SegregationEvaluation,
	evaluated_rules: list[tuple[str, SegregationRuleDefinition, tuple[str, ...], tuple[str, ...]]],
) -> SegregationDecision:
	conflicts: list[SegregationConflict] = []
	for name, definition, actor_fields, roles in sorted(evaluated_rules, key=lambda item: item[0]):
		matched_fields = sorted(set(actor_fields))
		matched_roles = sorted(set(roles))
		if not matched_fields and not matched_roles:
			continue
		reason = (
			"ACTOR_AND_ROLE"
			if matched_fields and matched_roles
			else ("ACTOR_REUSE" if matched_fields else "INCOMPATIBLE_ROLE")
		)
		conflicts.append(
			{
				"rule": name,
				"code": definition.code,
				"revision": definition.revision,
				"policy_digest": definition.policy_digest,
				"reason": reason,
				"matched_actor_fields": matched_fields,
				"matched_roles": matched_roles,
			}
		)
	if not evaluated_rules:
		conflicts.append(
			{
				"rule": None,
				"code": "NO_APPLICABLE_RULE",
				"reason": "NO_APPLICABLE_RULE",
				"matched_actor_fields": [],
				"matched_roles": [],
			}
		)
	decision: SegregationDecision = {
		"schema_version": SEGREGATION_SCHEMA_VERSION,
		"action": evaluation.action,
		"allowed": not conflicts,
		"rules_evaluated": len(evaluated_rules),
		"conflicts": conflicts,
		"decision_digest": "",
	}
	decision["decision_digest"] = fingerprint_json(
		{
			"decision": decision,
			"evaluated_policies": [
				{
					"rule": name,
					"code": definition.code,
					"revision": definition.revision,
					"policy_digest": definition.policy_digest,
				}
				for name, definition, _actor_fields, _roles in sorted(
					evaluated_rules, key=lambda item: item[0]
				)
			],
			"subject": evaluation.user,
			"doctype": evaluation.target_doctype,
			"docname": evaluation.docname,
		}
	)
	return decision


__all__ = [
	"ACTOR_FIELD_PATTERN",
	"MAX_SEGREGATION_SOURCES",
	"SEGREGATION_ACTIONS",
	"SEGREGATION_RULE_DOCTYPE",
	"SEGREGATION_SCHEMA_VERSION",
	"SegregationAction",
	"SegregationConflict",
	"SegregationContractError",
	"SegregationDecision",
	"SegregationEvaluation",
	"SegregationRuleDefinition",
	"SegregationRuleUpsert",
	"build_actor_fields",
	"build_conflicting_roles",
	"build_segregation_decision",
	"build_segregation_evaluation",
	"build_segregation_rule_definition",
	"build_segregation_rule_upsert",
	"normalize_segregation_action",
]
