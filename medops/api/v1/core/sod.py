from __future__ import annotations

import frappe

from medops.common.segregation import (
	SegregationContractError,
	build_segregation_evaluation,
	build_segregation_rule_upsert,
)
from medops.hrp_workflow_authorization.services.segregation import (
	get_segregation_rule,
	upsert_segregation_rule,
	validate_segregation,
)
from medops.services.audit_context import service_audit_scope
from medops.services.errors import raise_ione_error, require_authenticated_user


def _reject_unknown_arguments(arguments: dict[str, object]) -> None:
	if any(key != "cmd" for key in arguments):
		raise_ione_error("INVALID_REQUEST")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def validate(
	user: str | None = None,
	action: str | None = None,
	doctype: str | None = None,
	docname: str | None = None,
	correlation_id: str | None = None,
	**extra_arguments: object,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		_reject_unknown_arguments(extra_arguments)
		try:
			command = build_segregation_evaluation(
				user=user,
				action=action,
				doctype=doctype,
				docname=docname,
			)
		except SegregationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return validate_segregation(command, correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def upsert_rule(
	code: str | None = None,
	display_name: str | None = None,
	target_doctype: str | None = None,
	target_action: str | None = None,
	company: str | None = None,
	hospital: str | None = None,
	valid_from: str | None = None,
	actor_fields: str | list[str] | None = None,
	conflicting_roles: str | list[str] | None = None,
	organization_unit: str | None = None,
	include_descendants: bool | int | str = False,
	enabled: bool | int | str = True,
	valid_to: str | None = None,
	expected_revision: int | str = 0,
	rule_name: str | None = None,
	remarks: str | None = None,
	correlation_id: str | None = None,
	**extra_arguments: object,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		_reject_unknown_arguments(extra_arguments)
		try:
			command = build_segregation_rule_upsert(
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
				expected_revision=expected_revision,
				rule_name=rule_name,
				remarks=remarks,
			)
		except SegregationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return upsert_segregation_rule(command, idempotency_key=None, correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_rule(
	rule_name: str | None = None,
	correlation_id: str | None = None,
	**extra_arguments: object,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		_reject_unknown_arguments(extra_arguments)
		if not rule_name:
			raise_ione_error("INVALID_REQUEST")
		return get_segregation_rule(rule_name, correlation_id=correlation_id)


__all__ = ["get_rule", "upsert_rule", "validate"]
