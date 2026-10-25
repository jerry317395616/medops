from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import frappe

from medops.common.business_rules import RULE_ACTIONS, rule_payload
from medops.common.domain_service import normalize_sha256
from medops.common.organization import normalize_nonnegative_integer, normalize_required_text
from medops.hrp_workflow_authorization.services.business_rules import (
	EvaluateBusinessRules,
	RecordBusinessRuleSnapshot,
	UpsertBusinessRule,
	get_log,
	get_rule,
	list_logs,
)
from medops.services.audit_context import service_audit_scope
from medops.services.errors import raise_ione_error, require_authenticated_user


def _extra(extra: dict[str, object]) -> None:
	if set(extra) - {"cmd"}:
		raise_ione_error("INVALID_REQUEST")


def _execution(result: Any) -> dict[str, object]:
	return {
		**result.result,
		"correlation_id": result.correlation_id,
		"request_id": result.request_id,
		"idempotency_replayed": result.idempotency_replayed,
	}


def _source(doctype: object, name: object, action: object) -> dict[str, Any]:
	try:
		if action not in RULE_ACTIONS:
			raise ValueError("invalid action")
		return {
			"doctype": normalize_required_text(doctype, label="doctype", maximum=140),
			"name": normalize_required_text(name, label="name", maximum=140),
			"action": action,
		}
	except ValueError as exc:
		raise_ione_error("INVALID_REQUEST", cause=exc)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def upsert(
	policy: str | dict | None = None,
	expected_revision: int | str = 0,
	correlation_id: str | None = None,
	**extra: object,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		_extra(extra)
		try:
			if isinstance(policy, str):
				if len(policy.encode("utf-8")) > 32768:
					raise ValueError("policy too large")
				policy = json.loads(policy)
			command = {
				"policy": rule_payload(policy),
				"expected_revision": normalize_nonnegative_integer(
					expected_revision, label="expected_revision"
				),
			}
		except ValueError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return _execution(UpsertBusinessRule().execute(command, correlation_id=correlation_id))


@frappe.whitelist(allow_guest=True, methods=["POST"])
def evaluate(
	doctype: str | None = None,
	name: str | None = None,
	action: str | None = None,
	correlation_id: str | None = None,
	**extra: object,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		_extra(extra)
		return _execution(
			EvaluateBusinessRules().execute(_source(doctype, name, action), correlation_id=correlation_id)
		)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def record(
	doctype: str | None = None,
	name: str | None = None,
	action: str | None = None,
	expected_source_modified: str | None = None,
	expected_decision_digest: str | None = None,
	correlation_id: str | None = None,
	**extra: object,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		_extra(extra)
		command = _source(doctype, name, action)
		try:
			command.update(
				expected_source_modified=normalize_required_text(
					expected_source_modified, label="expected_source_modified", maximum=40
				),
				expected_decision_digest=normalize_sha256(
					expected_decision_digest, label="expected_decision_digest"
				),
			)
			if datetime.fromisoformat(command["expected_source_modified"]).tzinfo:
				raise ValueError("expected source time must use the site convention")
		except ValueError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return _execution(RecordBusinessRuleSnapshot().execute(command, correlation_id=correlation_id))


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get(
	rule_name: str | None = None, correlation_id: str | None = None, **extra: object
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		_extra(extra)
		if not rule_name or len(rule_name) > 140:
			raise_ione_error("INVALID_REQUEST")
		return get_rule(rule_name)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def execution_log(
	log_name: str | None = None, correlation_id: str | None = None, **extra: object
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		_extra(extra)
		if not log_name or len(log_name) > 140:
			raise_ione_error("INVALID_REQUEST")
		return get_log(log_name)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def logs(
	offset: int | str = 0,
	limit: int | str = 20,
	doctype: str | None = None,
	name: str | None = None,
	correlation_id: str | None = None,
	**extra: object,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		_extra(extra)
		try:
			start = normalize_nonnegative_integer(offset, label="offset")
			length = normalize_nonnegative_integer(limit, label="limit")
			if start > 10000 or not 1 <= length <= 50:
				raise ValueError("pagination exceeds limits")
		except ValueError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return list_logs(offset=start, limit=length, doctype=doctype, name=name)
