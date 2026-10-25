from __future__ import annotations

from typing import cast

import frappe

from medops.common.system_settings import (
	LOCKED_RELEASE_CHANNEL,
	SystemSettingsContractError,
	normalize_positive_integer,
	normalize_timeout,
)
from medops.hrp_foundation.doctype.hrp_system_settings.hrp_system_settings import (
	HRPSystemSettings,
)
from medops.services.audit_context import emit_audit_event


def ensure_system_settings() -> dict[str, object]:
	doc = cast(
		HRPSystemSettings,
		frappe.get_single("HRP System Settings"),
	)
	changed_fields: list[str] = []

	def set_if_different(fieldname: str, value: object) -> None:
		if doc.get(fieldname) != value:
			doc.set(fieldname, value)
			changed_fields.append(fieldname)

	set_if_different("release_channel", LOCKED_RELEASE_CHANNEL)
	set_if_different("strict_data_scope", 1)
	set_if_different("require_human_confirmation_for_ai", 1)
	version_rows = frappe.db.sql(
		"""
		SELECT value
		FROM `tabSingles`
		WHERE doctype = %s AND field = %s
		""",
		("HRP System Settings", "configuration_version"),
		as_dict=True,
	)
	if not version_rows:
		doc.configuration_version = 1
		changed_fields.append("configuration_version")
	else:
		try:
			normalize_positive_integer(
				version_rows[0].value,
				label="configuration_version",
			)
		except SystemSettingsContractError:
			set_if_different("configuration_version", 1)
	try:
		normalize_timeout(doc.integration_timeout_seconds or 30)
	except SystemSettingsContractError:
		set_if_different("integration_timeout_seconds", 30)
	if doc.default_hospital and not doc.default_company:
		set_if_different("default_hospital", None)

	if changed_fields:
		doc.flags.system_settings_repair = True
		doc.save(ignore_permissions=True)
		emit_audit_event(
			"system_settings_repaired",
			logger_name="medops.system_settings",
			changed_field_count=len(changed_fields),
			changed_fields=",".join(changed_fields),
			configuration_version=doc.configuration_version,
		)
	return {
		"changed": bool(changed_fields),
		"changed_fields": changed_fields,
		"configuration_version": int(doc.configuration_version or 1),
	}


__all__ = ["ensure_system_settings"]
