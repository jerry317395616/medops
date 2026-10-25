from __future__ import annotations

import frappe

from medops.common.approval_matrix import (
	ApprovalMatrixContractError,
	build_approval_evaluation,
	build_approval_matrix_steps,
	build_approval_matrix_upsert,
)
from medops.hrp_workflow_authorization.services.approval_matrix import (
	evaluate_approval_matrix,
	get_approval_matrix,
	upsert_approval_matrix,
)
from medops.services.audit_context import service_audit_scope
from medops.services.errors import raise_ione_error, require_authenticated_user


@frappe.whitelist(allow_guest=True, methods=["POST"])
def upsert(
	code: str,
	display_name: str,
	target_doctype: str,
	company: str,
	hospital: str,
	valid_from: str,
	steps: str | list[dict[str, object]],
	organization_unit: str | None = None,
	include_descendants: bool | int | str = False,
	priority: int | str = 0,
	dimensions: str | dict[str, object] | None = None,
	enabled: bool | int | str = True,
	valid_to: str | None = None,
	expected_revision: int | str = 0,
	matrix_name: str | None = None,
	remarks: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
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
				steps=build_approval_matrix_steps(steps),
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
	doctype: str,
	docname: str,
	amount: int | float | str,
	dimensions: str | dict[str, object],
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		try:
			query = build_approval_evaluation(
				doctype=doctype,
				docname=docname,
				amount=amount,
				dimensions=dimensions,
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
