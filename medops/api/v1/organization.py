from __future__ import annotations

import frappe

from medops.common.organization import (
	OrganizationContractError,
	build_hierarchy_replace,
	build_hospital_upsert,
	build_organization_version_create,
	build_organization_version_publish,
)
from medops.hrp_organization.services import (
	create_organization_version as create_organization_version_service,
)
from medops.hrp_organization.services import (
	get_organization_hierarchy as get_organization_hierarchy_service,
)
from medops.hrp_organization.services import (
	publish_organization_version as publish_organization_version_service,
)
from medops.hrp_organization.services import (
	replace_organization_hierarchy as replace_organization_hierarchy_service,
)
from medops.hrp_organization.services import (
	upsert_hospital as upsert_hospital_service,
)
from medops.services.audit_context import service_audit_scope
from medops.services.errors import raise_ione_error


@frappe.whitelist(methods=["POST"])
def upsert_hospital(
	code: str,
	company: str,
	display_name: str,
	expected_revision: int | str,
	enabled: bool | int | str = True,
	valid_from: str | None = None,
	valid_to: str | None = None,
	remarks: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			command = build_hospital_upsert(
				code=code,
				company=company,
				display_name=display_name,
				enabled=enabled,
				valid_from=valid_from,
				valid_to=valid_to,
				remarks=remarks,
				expected_revision=expected_revision,
			)
		except OrganizationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return upsert_hospital_service(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(methods=["POST"])
def create_organization_version(
	hospital: str,
	effective_from: str,
	version_label: str,
	remarks: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			command = build_organization_version_create(
				hospital=hospital,
				effective_from=effective_from,
				version_label=version_label,
				remarks=remarks,
			)
		except OrganizationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return create_organization_version_service(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(methods=["POST"])
def replace_organization_hierarchy(
	organization_version: str,
	expected_revision: int | str,
	nodes: str | list[dict[str, object]],
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			command = build_hierarchy_replace(
				organization_version=organization_version,
				expected_revision=expected_revision,
				nodes=nodes,
			)
		except OrganizationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return replace_organization_hierarchy_service(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(methods=["POST"])
def publish_organization_version(
	organization_version: str,
	expected_revision: int | str,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			command = build_organization_version_publish(
				organization_version=organization_version,
				expected_revision=expected_revision,
			)
		except OrganizationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return publish_organization_version_service(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(methods=["GET"])
def get_organization_hierarchy(
	organization_version: str | None = None,
	hospital: str | None = None,
	effective_on: str | None = None,
) -> dict[str, object]:
	if bool(organization_version) == bool(hospital):
		raise_ione_error("INVALID_REQUEST")
	return get_organization_hierarchy_service(
		organization_version=organization_version,
		hospital=hospital,
		effective_on=effective_on,
	)
