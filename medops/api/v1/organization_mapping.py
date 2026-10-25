from __future__ import annotations

import frappe

from medops.common.organization_mapping import (
	OrganizationMappingContractError,
	build_organization_mapping_resolve,
	build_organization_mapping_upsert,
)
from medops.hrp_organization.services import (
	resolve_organization_mapping as resolve_organization_mapping_service,
)
from medops.hrp_organization.services import (
	upsert_organization_mapping as upsert_organization_mapping_service,
)
from medops.services.audit_context import service_audit_scope
from medops.services.errors import raise_ione_error


@frappe.whitelist(methods=["POST"])
def upsert_organization_mapping(
	organization_version: str,
	organization_unit: str,
	expected_revision: int | str,
	department: str | None = None,
	cost_center: str | None = None,
	enabled: bool | int | str = True,
	remarks: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			command = build_organization_mapping_upsert(
				organization_version=organization_version,
				organization_unit=organization_unit,
				department=department,
				cost_center=cost_center,
				enabled=enabled,
				expected_revision=expected_revision,
				remarks=remarks,
			)
		except OrganizationMappingContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return upsert_organization_mapping_service(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(methods=["GET"])
def resolve_organization_mapping(
	organization_unit: str | None = None,
	hospital: str | None = None,
	unit_code: str | None = None,
	effective_on: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			command = build_organization_mapping_resolve(
				organization_unit=organization_unit,
				hospital=hospital,
				unit_code=unit_code,
				effective_on=effective_on,
			)
		except OrganizationMappingContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return resolve_organization_mapping_service(
			command,
			correlation_id=correlation_id,
		)
