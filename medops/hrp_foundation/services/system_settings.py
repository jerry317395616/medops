from __future__ import annotations

from typing import cast

import frappe

from medops.common.domain_service import DomainServiceDefinition
from medops.common.system_settings import (
	SystemSettingsState,
	SystemSettingsUpdate,
	changed_mutable_fields,
)
from medops.hrp_foundation.doctype.hrp_system_settings.hrp_system_settings import (
	HRPSystemSettings,
)
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_roles

SYSTEM_SETTINGS_ADMIN_ROLES = frozenset({"System Manager", "HRP System Manager"})


def _get_settings_document() -> HRPSystemSettings:
	return cast(
		HRPSystemSettings,
		frappe.get_single("HRP System Settings"),
	)


def _public_state(state: SystemSettingsState) -> dict[str, object]:
	return state.as_public_dict()


def get_system_settings() -> dict[str, object]:
	with service_audit_scope():
		require_roles(SYSTEM_SETTINGS_ADMIN_ROLES)
		state = _get_settings_document().as_contract_state()
		emit_audit_event(
			"system_settings_read",
			logger_name="medops.system_settings",
			configuration_version=state.configuration_version,
			enabled=state.enabled,
			default_company_configured=state.default_company is not None,
			default_hospital_configured=state.default_hospital is not None,
		)
		return _public_state(state)


class UpdateSystemSettingsService(DomainService[SystemSettingsUpdate]):
	definition = DomainServiceDefinition(
		name="hrp_foundation.system_settings.update",
		version=1,
		kind="command",
		required_roles=SYSTEM_SETTINGS_ADMIN_ROLES,
	)

	def request_payload(self, command: SystemSettingsUpdate) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: SystemSettingsUpdate) -> None:
		if command.default_company and not frappe.db.exists("Company", command.default_company):
			raise_ione_error("RESOURCE_NOT_FOUND")
		if command.default_hospital:
			hospital_company = frappe.db.get_value(
				"HRP Hospital",
				command.default_hospital,
				"company",
			)
			if not hospital_company:
				raise_ione_error("RESOURCE_NOT_FOUND")
			if hospital_company != command.default_company:
				raise_ione_error("CONFLICT")

	def perform(self, command: SystemSettingsUpdate) -> dict[str, object]:
		locked_version, _ = HRPSystemSettings.lock_configuration(command.expected_version)
		doc = _get_settings_document()
		current = doc.as_contract_state()
		changed_fields = changed_mutable_fields(current, command)
		if not changed_fields:
			emit_audit_event(
				"system_settings_unchanged",
				logger_name="medops.system_settings",
				configuration_version=current.configuration_version,
				changed_field_count=0,
			)
			return {
				**_public_state(current),
				"changed": False,
				"changed_fields": [],
			}

		doc.enabled = int(command.enabled)
		doc.default_company = command.default_company
		doc.default_hospital = command.default_hospital
		doc.integration_timeout_seconds = command.integration_timeout_seconds
		doc.remarks = command.remarks
		doc.configuration_version = locked_version
		doc.flags.locked_configuration_version = locked_version
		doc.save()
		updated = doc.as_contract_state()
		emit_audit_event(
			"system_settings_changed",
			logger_name="medops.system_settings",
			before_version=current.configuration_version,
			after_version=updated.configuration_version,
			changed_field_count=len(changed_fields),
			changed_fields=",".join(changed_fields),
		)
		return {
			**_public_state(updated),
			"changed": True,
			"changed_fields": list(changed_fields),
		}


def update_system_settings(
	command: SystemSettingsUpdate,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	execution = UpdateSystemSettingsService().execute(
		command,
		idempotency_key=idempotency_key,
		correlation_id=correlation_id,
	)
	return {
		**execution.result,
		"correlation_id": execution.correlation_id,
		"request_id": execution.request_id,
		"idempotency_replayed": execution.idempotency_replayed,
	}


__all__ = [
	"SYSTEM_SETTINGS_ADMIN_ROLES",
	"UpdateSystemSettingsService",
	"get_system_settings",
	"update_system_settings",
]
