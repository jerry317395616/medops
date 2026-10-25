from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypedDict, cast

import frappe
from frappe.model.document import Document

from medops.common.domain_service import DomainServiceDefinition, DomainServiceExecution
from medops.common.organization import OrganizationContractError, normalize_required_date
from medops.common.segregation import (
	SegregationEvaluation,
	SegregationRuleDefinition,
	SegregationRuleUpsert,
	build_segregation_decision,
)
from medops.hrp_workflow_authorization.doctype.hrp_segregation_rule.hrp_segregation_rule import (
	HRPSegregationRule,
)
from medops.hrp_workflow_authorization.permissions import has_scoped_permission
from medops.hrp_workflow_authorization.services.access_scope import dimension_fields_for
from medops.services.audit_context import emit_audit_event
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_roles

SEGREGATION_ADMIN_ROLES = frozenset({"System Manager", "HRP System Manager"})
SEGREGATION_READ_ROLES = frozenset({*SEGREGATION_ADMIN_ROLES, "HRP Auditor"})
MAX_SEGREGATION_RULE_CANDIDATES = 100
ACTION_PERMISSION_TYPES = {
	"amend": "create",
	"approve": "write",
	"cancel": "cancel",
	"create": "create",
	"pay": "write",
	"post": "write",
	"reconcile": "write",
	"release": "write",
	"review": "write",
	"submit": "submit",
}


class SegregationRuleDocumentPayload(TypedDict):
	doctype: str
	code: str
	display_name: str
	target_doctype: str
	target_action: str
	company: str
	hospital: str
	organization_unit: str | None
	include_descendants: int
	actor_fields_json: str
	conflicting_roles_json: str
	enabled: int
	valid_from: str
	valid_to: str | None
	revision: int
	remarks: str | None


@dataclass(frozen=True, slots=True)
class _DocumentContext:
	company: str
	hospital: str
	organization_unit: str
	effective_on: str


def _execution_payload(execution: DomainServiceExecution) -> dict[str, object]:
	return {
		**execution.result,
		"correlation_id": execution.correlation_id,
		"request_id": execution.request_id,
		"idempotency_replayed": execution.idempotency_replayed,
	}


def _rule_doc(name: str) -> HRPSegregationRule:
	if not frappe.db.exists("HRP Segregation Rule", name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(HRPSegregationRule, frappe.get_doc("HRP Segregation Rule", name))


def _document_payload(definition: SegregationRuleDefinition) -> SegregationRuleDocumentPayload:
	return {
		"doctype": "HRP Segregation Rule",
		"code": definition.code,
		"display_name": definition.display_name,
		"target_doctype": definition.target_doctype,
		"target_action": definition.target_action,
		"company": definition.company,
		"hospital": definition.hospital,
		"organization_unit": definition.organization_unit,
		"include_descendants": int(definition.include_descendants),
		"actor_fields_json": json.dumps(
			list(definition.actor_fields), ensure_ascii=False, separators=(",", ":")
		),
		"conflicting_roles_json": json.dumps(
			list(definition.conflicting_roles), ensure_ascii=False, separators=(",", ":")
		),
		"enabled": int(definition.enabled),
		"valid_from": definition.valid_from,
		"valid_to": definition.valid_to,
		"revision": 1,
		"remarks": definition.remarks,
	}


class UpsertSegregationRuleService(DomainService[SegregationRuleUpsert]):
	definition = DomainServiceDefinition(
		name="hrp_workflow_authorization.segregation_rule.upsert",
		version=1,
		kind="command",
		required_roles=SEGREGATION_ADMIN_ROLES,
	)

	def request_payload(self, command: SegregationRuleUpsert) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: SegregationRuleUpsert) -> None:
		definition = command.definition
		for doctype, name in (
			("Company", definition.company),
			("DocType", definition.target_doctype),
			("HRP Hospital", definition.hospital),
		):
			if not frappe.db.exists(doctype, name):
				raise_ione_error("RESOURCE_NOT_FOUND")
		if command.rule_name and not frappe.db.exists("HRP Segregation Rule", command.rule_name):
			raise_ione_error("RESOURCE_NOT_FOUND")

	def perform(self, command: SegregationRuleUpsert) -> dict[str, object]:
		definition = command.definition
		payload = _document_payload(definition)
		if command.rule_name is None:
			if command.expected_revision != 0 or frappe.db.exists("HRP Segregation Rule", definition.code):
				raise_ione_error("CONFLICT")
			doc = cast(HRPSegregationRule, frappe.get_doc(payload))
			doc.flags.segregation_rule_service_write = True
			try:
				doc.insert(ignore_permissions=True)
			except (frappe.DuplicateEntryError, frappe.UniqueValidationError) as exc:
				raise_ione_error("CONFLICT", cause=exc)
			emit_audit_event(
				"segregation_rule_created",
				logger_name="medops.segregation",
				policy_digest=doc.policy_digest,
				revision=1,
				actor_field_count=len(definition.actor_fields),
				conflicting_role_count=len(definition.conflicting_roles),
			)
			return {**doc.as_public_dict(), "changed": True, "changed_fields": ["created"]}

		current_revision = HRPSegregationRule.lock_revision(command.expected_revision, command.rule_name)
		doc = _rule_doc(command.rule_name)
		identity = (
			doc.code,
			doc.target_doctype,
			doc.target_action,
			doc.company,
			doc.hospital,
			doc.organization_unit or None,
		)
		requested = (
			definition.code,
			definition.target_doctype,
			definition.target_action,
			definition.company,
			definition.hospital,
			definition.organization_unit,
		)
		if identity != requested:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		changed_fields = [
			fieldname
			for fieldname, current, requested_value in (
				("display_name", doc.display_name, definition.display_name),
				("include_descendants", bool(doc.include_descendants), definition.include_descendants),
				("actor_fields_json", doc.actor_fields_json or "[]", payload["actor_fields_json"]),
				(
					"conflicting_roles_json",
					doc.conflicting_roles_json or "[]",
					payload["conflicting_roles_json"],
				),
				("enabled", bool(doc.enabled), definition.enabled),
				("valid_from", str(doc.valid_from), definition.valid_from),
				("valid_to", str(doc.valid_to) if doc.valid_to else None, definition.valid_to),
				("remarks", doc.remarks or None, definition.remarks),
			)
			if current != requested_value
		]
		if not changed_fields:
			return {**doc.as_public_dict(), "changed": False, "changed_fields": []}
		for fieldname in (
			"display_name",
			"include_descendants",
			"actor_fields_json",
			"conflicting_roles_json",
			"enabled",
			"valid_from",
			"valid_to",
			"remarks",
		):
			doc.set(fieldname, payload[fieldname])
		doc.flags.locked_revision = current_revision
		doc.flags.segregation_rule_service_write = True
		doc.save(ignore_permissions=True)
		emit_audit_event(
			"segregation_rule_changed",
			logger_name="medops.segregation",
			policy_digest=doc.policy_digest,
			before_revision=current_revision,
			after_revision=doc.revision,
			changed_field_count=len(changed_fields),
			changed_fields=",".join(changed_fields),
		)
		return {**doc.as_public_dict(), "changed": True, "changed_fields": changed_fields}


def upsert_segregation_rule(
	command: SegregationRuleUpsert,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		UpsertSegregationRuleService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def _configuration_date(value: object, *, label: str, required: bool = False) -> str | None:
	if value in (None, "") and not required:
		return None
	try:
		normalized = normalize_required_date(value, label=label)
	except OrganizationContractError as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)
	if len(normalized) != 10:
		raise_ione_error("CONFIGURATION_INVALID")
	return normalized


def _assert_document_context_references(context: _DocumentContext) -> None:
	if not frappe.db.exists("Company", context.company):
		raise_ione_error("CONFIGURATION_INVALID")
	hospital = frappe.db.get_value(
		"HRP Hospital",
		context.hospital,
		["company", "enabled", "valid_from", "valid_to"],
		as_dict=True,
	)
	unit = frappe.db.get_value(
		"HRP Organization Unit",
		context.organization_unit,
		[
			"company",
			"hospital",
			"enabled",
			"organization_version",
			"valid_from",
			"valid_to",
			"lft",
			"rgt",
		],
		as_dict=True,
	)
	if (
		not hospital
		or hospital.company != context.company
		or not bool(hospital.enabled)
		or not unit
		or unit.company != context.company
		or unit.hospital != context.hospital
		or not bool(unit.enabled)
		or not unit.organization_version
	):
		raise_ione_error("CONFIGURATION_INVALID")
	version = frappe.db.get_value(
		"HRP Organization Version",
		unit.organization_version,
		["company", "hospital", "docstatus", "status", "effective_from"],
		as_dict=True,
	)
	if (
		not version
		or version.company != context.company
		or version.hospital != context.hospital
		or int(version.docstatus or 0) != 1
		or version.status != "Published"
	):
		raise_ione_error("CONFIGURATION_INVALID")
	resolved_version = frappe.db.get_value(
		"HRP Organization Version",
		{
			"company": context.company,
			"hospital": context.hospital,
			"docstatus": 1,
			"status": "Published",
			"effective_from": ("<=", context.effective_on),
		},
		"name",
		order_by="effective_from desc, version_number desc",
	)
	if not resolved_version or resolved_version != unit.organization_version:
		raise_ione_error("CONFIGURATION_INVALID")
	for valid_from, valid_to in (
		(hospital.valid_from, hospital.valid_to),
		(unit.valid_from, unit.valid_to),
	):
		start = _configuration_date(valid_from, label="valid_from")
		end = _configuration_date(valid_to, label="valid_to")
		if (start and context.effective_on < start) or (end and context.effective_on > end):
			raise_ione_error("CONFIGURATION_INVALID")
	version_start = _configuration_date(
		version.effective_from,
		label="effective_from",
		required=True,
	)
	if version_start is None or context.effective_on < version_start:
		raise_ione_error("CONFIGURATION_INVALID")
	try:
		left = int(unit.lft)
		right = int(unit.rgt)
	except (TypeError, ValueError) as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)
	if left < 1 or right <= left:
		raise_ione_error("CONFIGURATION_INVALID")


def _document_context(doc: Document) -> _DocumentContext:
	mapping = dimension_fields_for(doc.doctype)
	if set(mapping) != {"company", "hospital", "organization_unit"}:
		raise_ione_error("CONFIGURATION_INVALID")
	company = str(doc.get(mapping["company"]) or "")
	hospital = str(doc.get(mapping["hospital"]) or "")
	organization_unit = str(doc.get(mapping["organization_unit"]) or "")
	if not company or not hospital or not organization_unit:
		raise_ione_error("CONFIGURATION_INVALID")
	fields = {field.fieldname for field in frappe.get_meta(doc.doctype).fields}
	date_field = next(
		(
			fieldname
			for fieldname in (
				"transaction_date",
				"posting_date",
				"effective_on",
				"request_date",
				"valid_from",
				"date",
			)
			if fieldname in fields and doc.get(fieldname)
		),
		None,
	)
	raw_effective_on = doc.get(date_field) if date_field else str(doc.creation or "")[:10]
	effective_on = _configuration_date(raw_effective_on, label="effective_on", required=True)
	if effective_on is None:
		raise_ione_error("CONFIGURATION_INVALID")
	context = _DocumentContext(company, hospital, organization_unit, effective_on)
	_assert_document_context_references(context)
	return context


def _rule_matches_unit(rule: HRPSegregationRule, organization_unit: str) -> bool:
	if not rule.organization_unit:
		return True
	if rule.organization_unit == organization_unit:
		return True
	if not bool(rule.include_descendants):
		return False
	root = frappe.db.get_value(
		"HRP Organization Unit",
		rule.organization_unit,
		["organization_version", "lft", "rgt"],
		as_dict=True,
	)
	target = frappe.db.get_value(
		"HRP Organization Unit",
		organization_unit,
		["company", "hospital", "enabled", "organization_version", "lft", "rgt"],
		as_dict=True,
	)
	if (
		not root
		or not target
		or target.company != rule.company
		or target.hospital != rule.hospital
		or not bool(target.enabled)
	):
		raise_ione_error("CONFIGURATION_INVALID")
	if root.organization_version != target.organization_version:
		return False
	if any(value is None for value in (root.lft, root.rgt, target.lft, target.rgt)):
		raise_ione_error("CONFIGURATION_INVALID")
	return int(root.lft) <= int(target.lft) and int(target.rgt) <= int(root.rgt)


def _applicable_rules(
	command: SegregationEvaluation,
	context: _DocumentContext,
) -> list[tuple[str, SegregationRuleDefinition]]:
	rows = frappe.get_all(
		"HRP Segregation Rule",
		filters={
			"target_doctype": command.target_doctype,
			"target_action": command.action,
			"company": context.company,
			"hospital": context.hospital,
			"enabled": 1,
			"valid_from": ("<=", context.effective_on),
		},
		or_filters=[
			["valid_to", "is", "not set"],
			["valid_to", ">=", context.effective_on],
		],
		fields=["name"],
		order_by="name asc",
		limit=MAX_SEGREGATION_RULE_CANDIDATES + 1,
	)
	if len(rows) > MAX_SEGREGATION_RULE_CANDIDATES:
		raise_ione_error("CONFIGURATION_INVALID")
	result: list[tuple[str, SegregationRuleDefinition]] = []
	for row in rows:
		doc = _rule_doc(str(row.name))
		definition = doc.as_runtime_definition()
		if doc.valid_to and str(doc.valid_to) < context.effective_on:
			continue
		if not _rule_matches_unit(doc, context.organization_unit):
			continue
		result.append((doc.name, definition))
	return result


class ValidateSegregationService(DomainService[SegregationEvaluation]):
	definition = DomainServiceDefinition(
		name="hrp_workflow_authorization.segregation.validate",
		version=1,
		kind="query",
		required_roles=SEGREGATION_ADMIN_ROLES,
	)

	def request_payload(self, command: SegregationEvaluation) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: SegregationEvaluation) -> None:
		if not frappe.db.exists("DocType", command.target_doctype):
			raise_ione_error("RESOURCE_NOT_FOUND")
		if not frappe.db.exists(command.target_doctype, command.docname):
			raise_ione_error("RESOURCE_NOT_FOUND")
		user = frappe.db.get_value("User", command.user, ["enabled", "user_type"], as_dict=True)
		if not user:
			raise_ione_error("RESOURCE_NOT_FOUND")
		if not bool(user.enabled) or user.user_type != "System User":
			raise_ione_error("INVALID_STATE_TRANSITION")
		doc = frappe.get_doc(command.target_doctype, command.docname)
		if not frappe.has_permission(command.target_doctype, ptype="read", doc=doc):
			raise_ione_error("PERMISSION_DENIED")
		if not has_scoped_permission(doc, user=frappe.session.user, ptype="read"):
			raise_ione_error("PERMISSION_DENIED")
		ptype = ACTION_PERMISSION_TYPES[command.action]
		if not frappe.has_permission(command.target_doctype, ptype=ptype, doc=doc, user=command.user):
			raise_ione_error("PERMISSION_DENIED")
		if not has_scoped_permission(doc, user=command.user, ptype=ptype):
			raise_ione_error("PERMISSION_DENIED")
		_document_context(doc)

	def perform(self, command: SegregationEvaluation) -> dict[str, object]:
		doc = frappe.get_doc(command.target_doctype, command.docname)
		context = _document_context(doc)
		user_roles = set(frappe.get_roles(command.user))
		evaluated: list[tuple[str, SegregationRuleDefinition, tuple[str, ...], tuple[str, ...]]] = []
		for name, definition in _applicable_rules(command, context):
			matched_fields = tuple(
				fieldname
				for fieldname in definition.actor_fields
				if str(doc.get(fieldname) or "") == command.user
			)
			matched_roles = tuple(role for role in definition.conflicting_roles if role in user_roles)
			evaluated.append((name, definition, matched_fields, matched_roles))
		decision = build_segregation_decision(evaluation=command, evaluated_rules=evaluated)
		emit_audit_event(
			"segregation_validated",
			logger_name="medops.segregation",
			target_doctype=command.target_doctype,
			action=command.action,
			allowed=decision["allowed"],
			rule_count=decision["rules_evaluated"],
			conflict_count=len(decision["conflicts"]),
			decision_digest=decision["decision_digest"],
		)
		return cast(dict[str, object], decision)


def validate_segregation(
	command: SegregationEvaluation,
	*,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		ValidateSegregationService().execute(
			command,
			correlation_id=correlation_id,
		)
	)


def get_segregation_rule(
	rule_name: str,
	*,
	correlation_id: object | None = None,
) -> dict[str, object]:
	from medops.services.audit_context import service_audit_scope

	with service_audit_scope(correlation_id) as context:
		require_roles(SEGREGATION_READ_ROLES)
		doc = _rule_doc(rule_name)
		definition = doc.as_runtime_definition()
		emit_audit_event(
			"segregation_rule_read",
			logger_name="medops.segregation",
			policy_digest=definition.policy_digest,
			revision=definition.revision,
		)
		return {
			"name": doc.name,
			**definition.as_public_dict(),
			"correlation_id": context.correlation_id,
			"request_id": context.request_id,
			"idempotency_replayed": False,
		}


__all__ = [
	"ACTION_PERMISSION_TYPES",
	"SEGREGATION_ADMIN_ROLES",
	"SEGREGATION_READ_ROLES",
	"UpsertSegregationRuleService",
	"ValidateSegregationService",
	"get_segregation_rule",
	"upsert_segregation_rule",
	"validate_segregation",
]
