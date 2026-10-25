from __future__ import annotations

import json
from typing import Any

import frappe

from medops.common.organization import normalize_nonnegative_integer, normalize_required_text
from medops.common.sla import SLAContractError, policy_payload
from medops.hrp_workflow_authorization.services.sla import CaseAction, UpsertPolicy, get_case, list_cases
from medops.services.audit_context import service_audit_scope
from medops.services.errors import raise_ione_error, require_authenticated_user


def _arguments(extra: dict[str, object]) -> None:
	if set(extra) - {"cmd"}:
		raise_ione_error("INVALID_REQUEST")


def _execution(execution: Any) -> dict[str, object]:
	return {
		**execution.result,
		"correlation_id": execution.correlation_id,
		"request_id": execution.request_id,
		"idempotency_replayed": execution.idempotency_replayed,
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def upsert_policy(
	policy: str | dict | None = None,
	expected_revision: int | str = 0,
	correlation_id: str | None = None,
	**extra: object,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		_arguments(extra)
		try:
			if isinstance(policy, str):
				if len(policy.encode("utf-8")) > 32768:
					raise SLAContractError("policy too large")
				policy = json.loads(policy)
			payload = policy_payload(policy)
			revision = normalize_nonnegative_integer(expected_revision, label="expected_revision")
		except ValueError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return _execution(
			UpsertPolicy().execute(
				{"policy": payload, "expected_revision": revision}, correlation_id=correlation_id
			)
		)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def act(
	case_name: str | None = None,
	action: str | None = None,
	expected_revision: int | str | None = None,
	reason: str | None = None,
	correlation_id: str | None = None,
	**extra: object,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		_arguments(extra)
		try:
			name = normalize_required_text(case_name, label="case_name", maximum=140)
			if action not in {"acknowledge", "process", "cancel"}:
				raise ValueError("invalid action")
			revision = normalize_nonnegative_integer(expected_revision, label="expected_revision")
			if revision < 1:
				raise ValueError("revision is required")
			normalized_reason = (
				normalize_required_text(reason, label="reason", maximum=500) if action == "cancel" else None
			)
			if action != "cancel" and reason:
				raise ValueError("reason is only valid for cancellation")
		except ValueError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return _execution(
			CaseAction().execute(
				{
					"case_name": name,
					"action": action,
					"expected_revision": revision,
					"reason": normalized_reason,
				},
				correlation_id=correlation_id,
			)
		)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get(
	case_name: str | None = None, correlation_id: str | None = None, **extra: object
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		_arguments(extra)
		if not case_name or len(case_name) > 140:
			raise_ione_error("INVALID_REQUEST")
		return get_case(case_name)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def list_my(
	offset: int | str = 0, limit: int | str = 20, correlation_id: str | None = None, **extra: object
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		_arguments(extra)
		try:
			offset_value = normalize_nonnegative_integer(offset, label="offset")
			limit_value = normalize_nonnegative_integer(limit, label="limit")
			if offset_value > 10000 or not 1 <= limit_value <= 50:
				raise ValueError("pagination out of range")
		except ValueError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return list_cases(offset=offset_value, limit=limit_value)
