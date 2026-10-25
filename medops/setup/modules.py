from __future__ import annotations

from typing import Any, cast

import frappe

from medops.common.constants import APP_NAME
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.errors import raise_ione_error
from medops.services.module_registry import ModuleSpec, load_module_registry

MODULE_SETTING_FIELDS = (
	"module_key",
	"domain_group",
	"label_cn",
	"sequence",
	"description",
)


def declared_modules() -> list[str]:
	return [row.module for row in load_module_registry().modules]


def _audit_sync(operation: str, report: dict[str, list[str]]) -> None:
	emit_audit_event(
		operation,
		logger_name="medops.module_registry",
		app=APP_NAME,
		created_count=len(report["created"]),
		updated_count=len(report.get("updated", [])),
		unchanged_count=len(report["unchanged"]),
	)


def sync_module_defs() -> dict[str, list[str]]:
	"""Create missing Module Def rows after checking all ownership conflicts."""
	with service_audit_scope():
		return _sync_module_defs()


def _sync_module_defs() -> dict[str, list[str]]:
	registry = load_module_registry()
	report: dict[str, list[str]] = {
		"created": [],
		"unchanged": [],
		"conflicts": [],
	}
	owners: dict[str, str | None] = {}
	for module in registry.modules:
		owner = cast(str | None, frappe.db.get_value("Module Def", module.module, "app_name"))
		owners[module.module] = owner
		if owner not in (None, APP_NAME):
			report["conflicts"].append(f"{module.module} -> {owner}")

	if report["conflicts"]:
		raise_ione_error(
			"CONFIGURATION_INVALID",
			cause=RuntimeError("Module Def ownership conflict"),
		)

	for module in registry.modules:
		if owners[module.module] == APP_NAME:
			report["unchanged"].append(module.module)
			continue
		frappe.get_doc(
			{
				"doctype": "Module Def",
				"module_name": module.module,
				"app_name": APP_NAME,
				"custom": 0,
			}
		).insert(ignore_permissions=True)
		report["created"].append(module.module)

	_audit_sync("module_def_sync", report)
	return report


def _module_setting_values(module: ModuleSpec) -> dict[str, Any]:
	return {
		"module_key": module.package,
		"domain_group": module.domain_group,
		"label_cn": module.label_cn,
		"sequence": module.sequence,
		"description": module.description,
	}


def sync_module_settings() -> dict[str, list[str]]:
	"""Upsert registry metadata while preserving each site's enabled choices."""
	with service_audit_scope():
		return _sync_module_settings()


def _sync_module_settings() -> dict[str, list[str]]:
	report: dict[str, list[str]] = {
		"created": [],
		"updated": [],
		"unchanged": [],
	}
	if not frappe.db.exists("DocType", "HRP Module Setting"):
		report["unchanged"].append("HRP Module Setting DocType is not installed")
		return report

	for module in load_module_registry().modules:
		values = _module_setting_values(module)
		if not frappe.db.exists("HRP Module Setting", module.module):
			frappe.get_doc(
				{
					"doctype": "HRP Module Setting",
					"module_name": module.module,
					"enabled": int(module.enabled_by_default),
					**values,
				}
			).insert(ignore_permissions=True)
			report["created"].append(module.module)
			continue

		doc = frappe.get_doc("HRP Module Setting", module.module)
		changed = False
		for fieldname in MODULE_SETTING_FIELDS:
			expected = values[fieldname]
			if doc.get(fieldname) == expected:
				continue
			doc.set(fieldname, expected)
			changed = True
		if changed:
			doc.save(ignore_permissions=True)
			report["updated"].append(module.module)
		else:
			report["unchanged"].append(module.module)

	_audit_sync("module_setting_sync", report)
	return report
