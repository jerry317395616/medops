from __future__ import annotations

from typing import Any

import frappe

from medops.common.approval_matrix import (
	ApprovalMatrixContractError,
	build_approval_evaluation,
	build_approval_matrix_steps,
	build_approval_matrix_upsert,
)
from medops.hrp_workflow_authorization.services.approval_matrix import (
	APPROVAL_MATRIX_ADMIN_ROLES,
	APPROVAL_MATRIX_EVALUATE_ROLES,
	evaluate_approval_matrix,
	get_approval_matrix,
	upsert_approval_matrix,
)
from medops.services.audit_context import service_audit_scope
from medops.services.errors import raise_ione_error, require_authenticated_user, require_roles


@frappe.whitelist(allow_guest=True, methods=["POST"])
def upsert(
	code: Any = None,
	display_name: Any = None,
	target_doctype: Any = None,
	company: Any = None,
	hospital: Any = None,
	valid_from: Any = None,
	steps: Any = None,
	organization_unit: Any = None,
	include_descendants: Any = False,
	priority: Any = 0,
	dimensions: Any = None,
	enabled: Any = True,
	valid_to: Any = None,
	expected_revision: Any = 0,
	matrix_name: Any = None,
	remarks: Any = None,
	correlation_id: Any = None,
	schema_version: Any = 1,
	amount_precision: Any = 2,
	**extra: Any,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		require_roles(APPROVAL_MATRIX_ADMIN_ROLES)
		if set(extra) - {"cmd"}:
			raise_ione_error("INVALID_REQUEST")
		try:
			command = build_approval_matrix_upsert(
				matrix_name=matrix_name,
				expected_revision=expected_revision,
				code=code,
				display_name=display_name,
				target_doctype=target_doctype,
				company=company,
				hospital=hospital,
				organization_unit=organization_unit,
				include_descendants=include_descendants,
				priority=priority,
				dimensions=dimensions,
				enabled=enabled,
				valid_from=valid_from,
				valid_to=valid_to,
				remarks=remarks,
				steps=build_approval_matrix_steps(
					steps, schema_version=schema_version, amount_precision=amount_precision
				),
				schema_version=schema_version,
				amount_precision=amount_precision,
			)
		except ApprovalMatrixContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return upsert_approval_matrix(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def evaluate(
	doctype: Any = None,
	docname: Any = None,
	amount: Any = None,
	dimensions: Any = None,
	correlation_id: Any = None,
	schema_version: Any = 1,
	amount_precision: Any = 2,
	**extra: Any,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		require_roles(APPROVAL_MATRIX_EVALUATE_ROLES)
		if set(extra) - {"cmd"}:
			raise_ione_error("INVALID_REQUEST")
		try:
			query = build_approval_evaluation(
				doctype=doctype,
				docname=docname,
				amount=amount,
				dimensions=dimensions,
				schema_version=schema_version,
				amount_precision=amount_precision,
			)
		except ApprovalMatrixContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return evaluate_approval_matrix(query, correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get(matrix_name: str, correlation_id: str | None = None) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		return get_approval_matrix(matrix_name, correlation_id=correlation_id)


__all__ = ["evaluate", "get", "upsert"]
