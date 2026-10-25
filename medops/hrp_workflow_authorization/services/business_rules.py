from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime

from medops.common.business_rules import (
	MAX_CHILD_ROWS,
	MAX_FIELD_PATHS,
	MAX_RULES,
	RULE_ACTIONS,
	BusinessRuleError,
	EvaluationBudget,
	evaluate_tree,
	rule_payload,
	scalar,
)
from medops.common.domain_service import DomainServiceDefinition, canonical_json, fingerprint_json
from medops.hrp_workflow_authorization.permissions import has_scoped_permission, scope_permission_query
from medops.hrp_workflow_authorization.services.access_scope import dimension_fields_for
from medops.hrp_workflow_authorization.services.rule_contracts import (
	can_read_fields,
	contracts_for_paths,
	require_field_read,
)
from medops.hrp_workflow_authorization.services.segregation import get_governed_document_context
from medops.services.audit_context import emit_audit_event, ensure_audit_context
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_roles

RULE = "HRP Business Rule"
LOG = "HRP Rule Execution Log"
ADMIN_ROLES = frozenset({"System Manager", "HRP System Manager"})
READ_ROLES = frozenset({*ADMIN_ROLES, "HRP User", "HRP Data Steward", "HRP Auditor", "HRP Integration User"})


def lock_rule_scope(source: Document, *, exclusive: bool = False) -> None:
	"""Shared fence for business actions; exclusive fence for rule configuration changes."""
	mapping = dimension_fields_for(source.doctype)
	if set(mapping) != {"company", "hospital", "organization_unit"}:
		raise_ione_error("CONFIGURATION_INVALID")
	query = (
		"SELECT name FROM `tabHRP Hospital` WHERE name=%s FOR UPDATE"
		if exclusive
		else "SELECT name FROM `tabHRP Hospital` WHERE name=%s LOCK IN SHARE MODE"
	)
	if not frappe.db.sql(query, source.get(mapping["hospital"])):
		raise_ione_error("RESOURCE_NOT_FOUND")


def require_source_read(source: Document) -> None:
	if not frappe.has_permission(source.doctype, ptype="read", doc=source) or not has_scoped_permission(
		source, ptype="read"
	):
		raise_ione_error("PERMISSION_DENIED")


def source_document(doctype: str, name: str, *, lock: bool = False) -> Document:
	if doctype in {RULE, LOG}:
		raise_ione_error("INVALID_REQUEST")
	if not frappe.db.exists("DocType", doctype) or not frappe.db.exists(doctype, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	doc = frappe.get_doc(doctype, name, for_update=lock)
	require_source_read(doc)
	return doc


def _ancestors(context: Any) -> tuple[str, ...]:
	result = []
	name = context.organization_unit
	version = None
	for _ in range(2000):
		if not name:
			return tuple(result)
		if name in result:
			raise_ione_error("CONFIGURATION_INVALID")
		row = frappe.db.get_value(
			"HRP Organization Unit",
			name,
			["company", "hospital", "enabled", "organization_version", "parent_organization_unit"],
			as_dict=True,
		)
		if (
			not row
			or row.company != context.company
			or row.hospital != context.hospital
			or not row.enabled
			or (version and row.organization_version != version)
		):
			raise_ione_error("CONFIGURATION_INVALID")
		version = row.organization_version
		result.append(name)
		name = row.parent_organization_unit
	raise_ione_error("CONFIGURATION_INVALID")


def _rules(source: Document, action: str, context: Any) -> list[Document]:
	ancestors = _ancestors(context)
	rows = frappe.db.sql(
		"""SELECT * FROM `tabHRP Business Rule`
		WHERE target_doctype=%(doctype)s AND target_action=%(action)s
		AND company=%(company)s AND hospital=%(hospital)s AND enabled=1
		AND valid_from<=%(date)s AND (valid_to IS NULL OR valid_to>=%(date)s)
		AND (organization_unit IS NULL OR organization_unit='' OR organization_unit=%(unit)s
		OR (include_descendants=1 AND organization_unit IN %(ancestors)s))
		ORDER BY priority DESC, name ASC LIMIT 65 LOCK IN SHARE MODE""",
		{
			"doctype": source.doctype,
			"action": action,
			"company": context.company,
			"hospital": context.hospital,
			"date": context.effective_on,
			"unit": context.organization_unit,
			"ancestors": ancestors,
		},
		as_dict=True,
	)
	if len(rows) > MAX_RULES:
		raise_ione_error("CONFIGURATION_INVALID")
	# A shared hospital fence cannot refresh an existing REPEATABLE READ snapshot.
	# Use the full current-read rows; a second get_doc() would read the old snapshot again.
	return [frappe.get_doc({"doctype": RULE, **dict(row)}) for row in rows]


def _normalized_value(value: object, contract: dict[str, Any]) -> object:
	if value in (None, ""):
		return value
	normalized = scalar(value, contract)
	if isinstance(normalized, Decimal):
		return format(normalized, "f")
	if isinstance(normalized, (datetime, date)):
		return normalized.isoformat()
	return normalized


def _source_values(source: Document, contracts: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], str]:
	values: dict[str, Any] = {}
	digest = hashlib.sha256()
	for path, contract in sorted(contracts.items()):
		if "." in path:
			continue
		digest.update(path.encode())
		if contract["kind"] != "table":
			values[path] = _normalized_value(source.get(path), contract)
			digest.update(canonical_json(values[path]).encode())
			continue
		rows = source.get(path) or []
		if not isinstance(rows, list) or len(rows) > MAX_CHILD_ROWS:
			raise BusinessRuleError("too many child rows")
		child_fields = {
			name.split(".")[1]: spec for name, spec in contracts.items() if name.startswith(path + ".")
		}
		values[path] = []
		for row in rows:
			if not hasattr(row, "get"):
				raise BusinessRuleError("invalid child row")
			item = {field: _normalized_value(row.get(field), spec) for field, spec in child_fields.items()}
			values[path].append(item)
			digest.update(canonical_json(item).encode())
			digest.update(b"\n")
	return values, fingerprint_json(
		{
			"doctype": source.doctype,
			"name": source.name,
			"modified": str(source.modified),
			"values_digest": digest.hexdigest(),
		}
	)


def evaluate_document(
	source: Document, action: str, *, lock: bool = False, enforce_read: bool = True
) -> tuple[dict[str, Any], list[str], Any]:
	if action not in RULE_ACTIONS:
		raise_ione_error("INVALID_REQUEST")
	if lock:
		lock_rule_scope(source)
	context = get_governed_document_context(source)
	docs = _rules(source, action, context)
	if docs or enforce_read:
		require_source_read(source)
	try:
		definitions = []
		contracts: dict[str, dict[str, Any]] = {}
		for doc in docs:
			payload, fields = doc.runtime_definition()
			contracts.update(fields)
			definitions.append((doc, payload, fields))
		if len(contracts) > MAX_FIELD_PATHS:
			raise BusinessRuleError("too many source fields")
		require_field_read(source, contracts)
		values, source_digest = _source_values(source, contracts)
		budget = EvaluationBudget()
		results = []
		for doc, payload, fields in definitions:
			applies = evaluate_tree(json.loads(payload["when_json"]), values, fields, budget=budget)
			passed = (
				evaluate_tree(json.loads(payload["assertion_json"]), values, fields, budget=budget)
				if applies
				else False
			)
			status = (
				"Skipped"
				if not applies
				else "Passed"
				if passed
				else "Blocked"
				if payload["severity"] == "Block"
				else "Warned"
			)
			results.append(
				{
					"rule": doc.name,
					"revision": int(doc.revision),
					"policy_digest": doc.policy_digest,
					"status": status,
				}
			)
	except (ValueError, TypeError, KeyError) as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)
	applicable = [row for row in results if row["status"] != "Skipped"]
	outcome = (
		"Unconfigured"
		if not applicable
		else "Blocked"
		if any(row["status"] == "Blocked" for row in applicable)
		else "Warned"
		if any(row["status"] == "Warned" for row in applicable)
		else "Passed"
	)
	rule_set_digest = fingerprint_json(
		[
			{"rule": doc.name, "revision": int(doc.revision), "policy_digest": doc.policy_digest}
			for doc in docs
		]
	)
	decision = {
		"schema_version": 1,
		"reference_doctype": source.doctype,
		"reference_name": source.name,
		"source_modified": str(source.modified),
		"target_action": action,
		"configured": bool(docs),
		"allowed": outcome in {"Passed", "Warned"},
		"outcome": outcome,
		"rules_evaluated": len(results),
		"rules": results,
		"source_digest": source_digest,
		"rule_set_digest": rule_set_digest,
	}
	decision["decision_digest"] = fingerprint_json({"actor": frappe.session.user, **decision})
	emit_audit_event(
		"business_rules_evaluated",
		logger_name="medops.business_rules",
		outcome=outcome,
		rules_evaluated=len(results),
		rule_set_digest=rule_set_digest,
	)
	return decision, sorted(contracts), context


def append_evidence(
	source: Document, decision: dict[str, Any], fields: list[str], context: Any, *, purpose: str
) -> str:
	audit = ensure_audit_context()
	event_key = fingerprint_json(
		[
			audit.request_id,
			source.doctype,
			source.name,
			decision["target_action"],
			decision["decision_digest"],
			purpose,
		]
	)
	existing = frappe.db.get_value(LOG, {"event_key": event_key}, "name")
	if existing:
		return existing
	doc = frappe.get_doc(
		{
			"doctype": LOG,
			"event_key": event_key,
			"evaluated_at": now_datetime(),
			"purpose": purpose,
			"reference_doctype": source.doctype,
			"reference_name": source.name,
			"source_modified": source.modified,
			"target_action": decision["target_action"],
			"company": context.company,
			"hospital": context.hospital,
			"organization_unit": context.organization_unit,
			"actor": frappe.session.user,
			"outcome": decision["outcome"],
			"decision_digest": decision["decision_digest"],
			"rule_set_digest": decision["rule_set_digest"],
			"source_digest": decision["source_digest"],
			"field_refs_json": canonical_json(fields),
			"rule_results_json": canonical_json(decision["rules"]),
			"correlation_id": audit.correlation_id,
			"request_id": audit.request_id,
		}
	)
	doc.flags.business_rule_log_append = True
	doc.insert(ignore_permissions=True)
	return doc.name


def enforce_business_rules(source: Document, action: str, *, required: bool = True) -> None:
	decision, fields, context = evaluate_document(source, action, lock=True, enforce_read=False)
	if not decision["configured"] and not required:
		return
	if not decision["allowed"]:
		# A failed business transaction rolls back its DB evidence too. The redacted audit event
		# above survives; explicit snapshot recording is available for retained denial evidence.
		raise_ione_error("INVALID_STATE_TRANSITION")
	append_evidence(source, decision, fields, context, purpose="Guard")


class UpsertBusinessRule(DomainService[dict[str, Any]]):
	definition = DomainServiceDefinition(
		name="hrp_workflow_authorization.business_rule.upsert",
		version=1,
		kind="command",
		required_roles=ADMIN_ROLES,
	)

	def request_payload(self, command: dict[str, Any]) -> dict[str, object]:
		return command

	def validate(self, command: dict[str, Any]) -> None:
		p = command["policy"]
		for doctype, name in (
			("Company", p["company"]),
			("HRP Hospital", p["hospital"]),
			("DocType", p["target_doctype"]),
			*([("HRP Organization Unit", p["organization_unit"])] if p["organization_unit"] else []),
		):
			if not frappe.db.exists(doctype, name):
				raise_ione_error("RESOURCE_NOT_FOUND")

	def perform(self, command: dict[str, Any]) -> dict[str, object]:
		p = rule_payload(command["policy"])
		probe = frappe.get_doc({"doctype": RULE, **p})
		lock_rule_scope(probe, exclusive=True)
		exists = frappe.db.exists(RULE, p["code"])
		if exists:
			doc = frappe.get_doc(RULE, p["code"], for_update=True)
			if int(doc.revision) != command["expected_revision"]:
				raise_ione_error("CONFLICT")
			doc.update(p)
		else:
			if command["expected_revision"] != 0:
				raise_ione_error("CONFLICT")
			doc = probe
		doc.flags.business_rule_service_write = True
		doc.save(ignore_permissions=True) if exists else doc.insert(ignore_permissions=True)
		emit_audit_event(
			"business_rule_saved",
			logger_name="medops.business_rules",
			policy_digest=doc.policy_digest,
			revision=int(doc.revision),
			enabled=bool(doc.enabled),
		)
		return {
			"name": doc.name,
			"revision": int(doc.revision),
			"policy_digest": doc.policy_digest,
			"policy": p,
		}


class EvaluateBusinessRules(DomainService[dict[str, Any]]):
	definition = DomainServiceDefinition(
		name="hrp_workflow_authorization.business_rule.evaluate",
		version=1,
		kind="query",
		required_roles=READ_ROLES,
	)

	def request_payload(self, command: dict[str, Any]) -> dict[str, object]:
		return command

	def validate(self, command: dict[str, Any]) -> None:
		source_document(command["doctype"], command["name"])

	def perform(self, command: dict[str, Any]) -> dict[str, object]:
		return evaluate_document(source_document(command["doctype"], command["name"]), command["action"])[0]


class RecordBusinessRuleSnapshot(DomainService[dict[str, Any]]):
	definition = DomainServiceDefinition(
		name="hrp_workflow_authorization.business_rule.record",
		version=1,
		kind="command",
		required_roles=READ_ROLES,
	)

	def request_payload(self, command: dict[str, Any]) -> dict[str, object]:
		return {**command, "actor": frappe.session.user}

	def validate(self, command: dict[str, Any]) -> None:
		source_document(command["doctype"], command["name"])

	def perform(self, command: dict[str, Any]) -> dict[str, object]:
		source = source_document(command["doctype"], command["name"], lock=True)
		decision, fields, context = evaluate_document(source, command["action"], lock=True)
		if (
			get_datetime(source.modified) != get_datetime(command["expected_source_modified"])
			or decision["decision_digest"] != command["expected_decision_digest"]
		):
			raise_ione_error("CONFLICT")
		return {
			"log": append_evidence(source, decision, fields, context, purpose="Snapshot"),
			"historical_evidence_only": True,
		}


def get_rule(name: str) -> dict[str, object]:
	require_roles({*ADMIN_ROLES, "HRP Auditor"})
	if not frappe.db.exists(RULE, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	doc = frappe.get_doc(RULE, name)
	p, _ = doc.runtime_definition()
	return {"name": doc.name, "policy": p, "revision": int(doc.revision), "policy_digest": doc.policy_digest}


def _log_readable(doc: Document, user: str | None = None) -> bool:
	user = user or frappe.session.user
	if not doc.reference_doctype or not doc.reference_name or doc.reference_doctype in {RULE, LOG}:
		return False
	if not frappe.db.exists("DocType", doc.reference_doctype) or not frappe.db.exists(
		doc.reference_doctype, doc.reference_name
	):
		return False
	try:
		source = frappe.get_doc(doc.reference_doctype, doc.reference_name)
		if not frappe.has_permission(
			source.doctype, ptype="read", doc=source, user=user
		) or not has_scoped_permission(source, user=user, ptype="read"):
			return False
		paths = json.loads(doc.field_refs_json)
		if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
			return False
		return can_read_fields(source, contracts_for_paths(source.doctype, paths), user=user)
	except (ValueError, KeyError, TypeError, frappe.DoesNotExistError):
		return False


def _require_log_read(doc: Document) -> None:
	if not _log_readable(doc):
		raise_ione_error("PERMISSION_DENIED")


def _log_public(doc: Document) -> dict[str, object]:
	return {
		"name": doc.name,
		"reference_doctype": doc.reference_doctype,
		"reference_name": doc.reference_name,
		"source_modified": str(doc.source_modified),
		"target_action": doc.target_action,
		"evaluated_at": str(doc.evaluated_at),
		"purpose": doc.purpose,
		"recorded_outcome": doc.outcome,
		"decision_digest": doc.decision_digest,
		"rules": json.loads(doc.rule_results_json),
		"historical_evidence_only": True,
	}


def get_log(name: str) -> dict[str, object]:
	require_roles(READ_ROLES)
	if not frappe.db.exists(LOG, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	doc = frappe.get_doc(LOG, name)
	_require_log_read(doc)
	return _log_public(doc)


def list_logs(
	*, offset: int = 0, limit: int = 20, doctype: str | None = None, name: str | None = None
) -> dict[str, object]:
	require_roles(READ_ROLES)
	condition = scope_permission_query(LOG)
	parts = [condition] if condition else []
	parameters: list[Any] = []
	if bool(doctype) != bool(name):
		raise_ione_error("INVALID_REQUEST")
	if doctype and name:
		source_document(doctype, name)
		parts.append("reference_doctype=%s AND reference_name=%s")
		parameters.extend([doctype, name])
	parameters.extend([limit, offset])
	rows = frappe.db.sql(
		"SELECT name FROM `tabHRP Rule Execution Log`"
		+ (" WHERE " + " AND ".join(parts) if parts else "")
		+ " ORDER BY evaluated_at DESC, name DESC LIMIT %s OFFSET %s",
		parameters,
		as_dict=True,
	)
	items = []
	for row in rows:
		doc = frappe.get_doc(LOG, row.name)
		if _log_readable(doc):
			items.append(_log_public(doc))
	return {"items": items, "next_offset": offset + len(rows) if len(rows) == limit else None}


def log_query(user: str | None = None) -> str:
	# Generic lists cannot apply dynamic source field permissions. Others use the scoped facade.
	return "" if (user or frappe.session.user) == "Administrator" else "1=0"


def log_permission(doc: Document, user: str | None = None, ptype: str | None = None, **kwargs: Any) -> bool:
	del kwargs
	if ptype not in {None, "read", "select"}:
		return False
	return _log_readable(doc, user)
