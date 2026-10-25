from __future__ import annotations

from typing import TypedDict

import frappe
from frappe.utils import cint

from medops.hrp_foundation.services import set_module_enabled as set_module_enabled_service
from medops.services.audit_context import service_audit_scope
from medops.services.errors import (
	require_authenticated_user,
)
from medops.services.module_registry import load_module_registry


class ModuleView(TypedDict):
	module: str
	module_key: str
	domain_group: str
	label_cn: str
	enabled: bool
	sequence: int
	description: str


@frappe.whitelist(methods=["GET"])
def list_modules() -> list[ModuleView]:
	with service_audit_scope():
		require_authenticated_user()
		registry = load_module_registry()
		settings = {}
		if frappe.db.exists("DocType", "HRP Module Setting"):
			settings = {
				row.module_name: row
				for row in frappe.get_all(
					"HRP Module Setting",
					fields=[
						"module_name",
						"module_key",
						"domain_group",
						"label_cn",
						"enabled",
						"sequence",
						"description",
					],
				)
			}

		result: list[ModuleView] = []
		for module in registry.modules:
			row = settings.get(module.module)
			result.append(
				{
					"module": module.module,
					"module_key": row.module_key if row else module.package,
					"domain_group": row.domain_group if row else module.domain_group,
					"label_cn": row.label_cn if row else module.label_cn,
					"enabled": bool(row.enabled) if row else module.enabled_by_default,
					"sequence": row.sequence if row else module.sequence,
					"description": row.description if row else module.description,
				}
			)
		return sorted(result, key=lambda item: (item["sequence"], item["module"]))


@frappe.whitelist(methods=["POST"])
def set_module_enabled(
	module_name: str,
	enabled: int | str | bool,
	correlation_id: str | None = None,
) -> dict[str, object]:
	"""Execute the module command through the shared domain-service contract."""
	return set_module_enabled_service(
		module_name,
		bool(cint(enabled)),
		idempotency_key=None,
		correlation_id=correlation_id,
	)
