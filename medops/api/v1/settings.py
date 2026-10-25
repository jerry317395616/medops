from __future__ import annotations

import frappe

from medops.common.system_settings import (
	SystemSettingsContractError,
	build_system_settings_update,
)
from medops.hrp_foundation.services import (
	get_system_settings as get_system_settings_service,
)
from medops.hrp_foundation.services import (
	update_system_settings as update_system_settings_service,
)
from medops.services.audit_context import service_audit_scope
from medops.services.errors import raise_ione_error


@frappe.whitelist(methods=["GET"])
def get_system_settings() -> dict[str, object]:
	return get_system_settings_service()


@frappe.whitelist(methods=["POST"])
def update_system_settings(
	enabled: bool | int | str,
	integration_timeout_seconds: int | str,
	expected_version: int | str,
	default_company: str | None = None,
	default_hospital: str | None = None,
	remarks: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		try:
			command = build_system_settings_update(
				enabled=enabled,
				default_company=default_company,
				default_hospital=default_hospital,
				integration_timeout_seconds=integration_timeout_seconds,
				remarks=remarks,
				expected_version=expected_version,
			)
		except SystemSettingsContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return update_system_settings_service(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)
