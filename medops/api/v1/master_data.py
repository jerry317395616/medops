from __future__ import annotations

import frappe

from medops.common.data_quality import (
	DataQualityContractError,
	build_data_quality_evaluate,
	build_data_quality_rule_upsert,
)
from medops.common.external_code_mapping import (
	ExternalCodeMappingContractError,
	build_external_code_mapping_resolve,
	build_external_code_mapping_upsert,
	build_internal_code_mapping_resolve,
)
from medops.common.master_data import (
	MasterDataContractError,
	build_master_data_domain_upsert,
	build_master_data_request_review,
	build_master_data_request_submit,
	build_master_data_request_upsert,
)
from medops.hrp_master_data.services import (
	evaluate_data_quality as evaluate_data_quality_service,
)
from medops.hrp_master_data.services import (
	get_data_quality_issue as get_data_quality_issue_service,
)
from medops.hrp_master_data.services import (
	get_master_data_request as get_master_data_request_service,
)
from medops.hrp_master_data.services import (
	resolve_external_code_mapping as resolve_external_code_mapping_service,
)
from medops.hrp_master_data.services import (
	resolve_internal_code_mapping as resolve_internal_code_mapping_service,
)
from medops.hrp_master_data.services import (
	review_master_data_request as review_master_data_request_service,
)
from medops.hrp_master_data.services import (
	save_master_data_request as save_master_data_request_service,
)
from medops.hrp_master_data.services import (
	submit_master_data_request as submit_master_data_request_service,
)
from medops.hrp_master_data.services import (
	upsert_data_quality_rule as upsert_data_quality_rule_service,
)
from medops.hrp_master_data.services import (
	upsert_external_code_mapping as upsert_external_code_mapping_service,
)
from medops.hrp_master_data.services import (
	upsert_master_data_domain as upsert_master_data_domain_service,
)
from medops.services.audit_context import service_audit_scope
from medops.services.errors import raise_ione_error


@frappe.whitelist(methods=["POST"])
def upsert_master_data_domain(
	code: str,
	display_name: str,
	target_doctype: str,
	expected_revision: int | str,
	enabled: bool | int | str = True,
	remarks: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			command = build_master_data_domain_upsert(
				code=code,
				display_name=display_name,
				target_doctype=target_doctype,
				enabled=enabled,
				expected_revision=expected_revision,
				remarks=remarks,
			)
		except MasterDataContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return upsert_master_data_domain_service(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(methods=["POST"])
def save_master_data_request(
	master_data_domain: str,
	company: str,
	hospital: str,
	organization_unit: str,
	operation: str,
	subject: str,
	effective_on: str,
	changes: str | list[dict[str, object]],
	expected_revision: int | str = 0,
	request_name: str | None = None,
	target_name: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			command = build_master_data_request_upsert(
				request_name=request_name,
				master_data_domain=master_data_domain,
				company=company,
				hospital=hospital,
				organization_unit=organization_unit,
				operation=operation,
				target_name=target_name,
				subject=subject,
				effective_on=effective_on,
				changes=changes,
				expected_revision=expected_revision,
			)
		except MasterDataContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return save_master_data_request_service(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(methods=["POST"])
def submit_master_data_request(
	request_name: str,
	expected_revision: int | str,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			command = build_master_data_request_submit(
				request_name=request_name,
				expected_revision=expected_revision,
			)
		except MasterDataContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return submit_master_data_request_service(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(methods=["POST"])
def review_master_data_request(
	request_name: str,
	expected_revision: int | str,
	decision: str,
	reason: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			command = build_master_data_request_review(
				request_name=request_name,
				expected_revision=expected_revision,
				decision=decision,
				reason=reason,
			)
		except MasterDataContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return review_master_data_request_service(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(methods=["GET"])
def get_master_data_request(
	request_name: str,
	correlation_id: str | None = None,
) -> dict[str, object]:
	return get_master_data_request_service(
		request_name=request_name,
		correlation_id=correlation_id,
	)


@frappe.whitelist(methods=["POST"])
def upsert_external_code_mapping(
	master_data_domain: str,
	company: str,
	hospital: str,
	external_system: str,
	external_code: str,
	internal_name: str,
	valid_from: str,
	organization_unit: str | None = None,
	external_label: str | None = None,
	enabled: bool | int | str = True,
	valid_to: str | None = None,
	expected_revision: int | str = 0,
	mapping_name: str | None = None,
	remarks: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			command = build_external_code_mapping_upsert(
				mapping_name=mapping_name,
				master_data_domain=master_data_domain,
				company=company,
				hospital=hospital,
				organization_unit=organization_unit,
				external_system=external_system,
				external_code=external_code,
				external_label=external_label,
				internal_name=internal_name,
				enabled=enabled,
				valid_from=valid_from,
				valid_to=valid_to,
				expected_revision=expected_revision,
				remarks=remarks,
			)
		except ExternalCodeMappingContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return upsert_external_code_mapping_service(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(methods=["GET"])
def resolve_external_code_mapping(
	master_data_domain: str,
	company: str,
	hospital: str,
	external_system: str,
	external_code: str,
	effective_on: str,
	organization_unit: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			query = build_external_code_mapping_resolve(
				master_data_domain=master_data_domain,
				company=company,
				hospital=hospital,
				organization_unit=organization_unit,
				external_system=external_system,
				external_code=external_code,
				effective_on=effective_on,
			)
		except ExternalCodeMappingContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return resolve_external_code_mapping_service(
			query,
			correlation_id=correlation_id,
		)


@frappe.whitelist(methods=["GET"])
def resolve_internal_code_mapping(
	master_data_domain: str,
	company: str,
	hospital: str,
	external_system: str,
	internal_name: str,
	effective_on: str,
	organization_unit: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			query = build_internal_code_mapping_resolve(
				master_data_domain=master_data_domain,
				company=company,
				hospital=hospital,
				organization_unit=organization_unit,
				external_system=external_system,
				internal_name=internal_name,
				effective_on=effective_on,
			)
		except ExternalCodeMappingContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return resolve_internal_code_mapping_service(
			query,
			correlation_id=correlation_id,
		)


@frappe.whitelist(methods=["POST"])
def upsert_data_quality_rule(
	code: str,
	display_name: str,
	master_data_domain: str,
	company: str,
	hospital: str,
	target_field: str,
	rule_type: str,
	valid_from: str,
	parameters: str | dict[str, object] | None = None,
	severity: str = "Major",
	enabled: bool | int | str = True,
	valid_to: str | None = None,
	organization_unit: str | None = None,
	expected_revision: int | str = 0,
	rule_name: str | None = None,
	remarks: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			command = build_data_quality_rule_upsert(
				rule_name=rule_name,
				code=code,
				display_name=display_name,
				master_data_domain=master_data_domain,
				company=company,
				hospital=hospital,
				organization_unit=organization_unit,
				target_field=target_field,
				rule_type=rule_type,
				parameters=parameters,
				severity=severity,
				enabled=enabled,
				valid_from=valid_from,
				valid_to=valid_to,
				expected_revision=expected_revision,
				remarks=remarks,
			)
		except DataQualityContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return upsert_data_quality_rule_service(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(methods=["POST"])
def evaluate_data_quality(
	rule_name: str,
	target_name: str,
	effective_on: str,
	expected_rule_revision: int | str,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			command = build_data_quality_evaluate(
				rule_name=rule_name,
				target_name=target_name,
				effective_on=effective_on,
				expected_rule_revision=expected_rule_revision,
			)
		except DataQualityContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return evaluate_data_quality_service(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(methods=["GET"])
def get_data_quality_issue(
	issue_name: str,
	correlation_id: str | None = None,
) -> dict[str, object]:
	return get_data_quality_issue_service(
		issue_name=issue_name,
		correlation_id=correlation_id,
	)
