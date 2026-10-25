from __future__ import annotations

import frappe

from medops.common.delegation import (
	DelegationContractError,
	build_delegation_create,
	build_delegation_revoke,
)
from medops.hrp_workflow_authorization.services.delegation import (
	create_delegation,
	get_delegation,
	revoke_delegation,
)
from medops.services.audit_context import service_audit_scope
from medops.services.errors import raise_ione_error, require_authenticated_user


@frappe.whitelist(allow_guest=True, methods=["POST"])
def create(
	matrix: str | None = None,
	from_user: str | None = None,
	to_user: str | None = None,
	valid_from: str | None = None,
	valid_to: str | None = None,
	step_sequence: int | str | None = None,
	reason: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		try:
			command = build_delegation_create(
				matrix=matrix,
				from_user=from_user,
				to_user=to_user,
				valid_from=valid_from,
				valid_to=valid_to,
				step_sequence=step_sequence,
				reason=reason,
			)
		except DelegationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return create_delegation(command, idempotency_key=None, correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def revoke(
	delegation: str | None = None,
	reason: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		try:
			command = build_delegation_revoke(delegation=delegation, reason=reason)
		except DelegationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return revoke_delegation(command, idempotency_key=None, correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get(delegation: str | None = None, correlation_id: str | None = None) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		if not delegation:
			raise_ione_error("INVALID_REQUEST")
		return get_delegation(delegation, correlation_id=correlation_id)


__all__ = ["create", "get", "revoke"]
