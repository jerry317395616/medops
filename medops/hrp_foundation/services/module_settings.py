from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import frappe

from medops.common.domain_service import DomainServiceDefinition
from medops.services.audit_context import emit_audit_event
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error
from medops.services.module_registry import load_module_registry

if TYPE_CHECKING:
	from medops.hrp_foundation.doctype.hrp_module_setting.hrp_module_setting import (
		HRPModuleSetting,
	)

MODULE_ADMIN_ROLES = frozenset({"System Manager", "HRP System Manager"})


@dataclass(frozen=True, slots=True)
class SetModuleEnabledCommand:
	module_name: str
	enabled: bool


class SetModuleEnabledService(DomainService[SetModuleEnabledCommand]):
	definition = DomainServiceDefinition(
		name="hrp_foundation.module_setting.set_enabled",
		version=1,
		kind="command",
		required_roles=MODULE_ADMIN_ROLES,
	)

	def request_payload(self, command: SetModuleEnabledCommand) -> dict[str, object]:
		return {
			"module_name": command.module_name,
			"enabled": command.enabled,
		}

	def validate(self, command: SetModuleEnabledCommand) -> None:
		modules = {row.module for row in load_module_registry().modules}
		if command.module_name not in modules:
			raise_ione_error("RESOURCE_NOT_FOUND")

	def perform(self, command: SetModuleEnabledCommand) -> dict[str, object]:
		doc = cast(
			"HRPModuleSetting",
			frappe.get_doc("HRP Module Setting", command.module_name),
		)
		desired = int(command.enabled)
		current = int(bool(doc.enabled))
		if desired == current:
			emit_audit_event(
				"module_enabled_unchanged",
				logger_name="medops.module_registry",
				module=command.module_name,
				enabled=bool(current),
			)
			return {
				"module": command.module_name,
				"enabled": bool(current),
				"changed": False,
			}

		doc.enabled = desired
		doc.save()
		emit_audit_event(
			"module_enabled_changed",
			logger_name="medops.module_registry",
			module=command.module_name,
			before=bool(current),
			after=bool(desired),
		)
		return {
			"module": command.module_name,
			"enabled": bool(desired),
			"changed": True,
		}


def set_module_enabled(
	module_name: str,
	enabled: bool,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	execution = SetModuleEnabledService().execute(
		SetModuleEnabledCommand(module_name=module_name, enabled=enabled),
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
	"MODULE_ADMIN_ROLES",
	"SetModuleEnabledCommand",
	"SetModuleEnabledService",
	"set_module_enabled",
]
