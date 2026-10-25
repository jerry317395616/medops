from __future__ import annotations

from hashlib import sha256
from typing import cast

import frappe

from medops.hrp_foundation.doctype.hrp_system_settings.hrp_system_settings import (
	HRPSystemSettings,
)
from medops.hrp_organization.doctype.hrp_hospital.hrp_hospital import HRPHospital
from medops.services.audit_context import emit_audit_event
from medops.services.errors import raise_ione_error


def _legacy_hospital_code(company: str, legacy_identifier: str) -> str:
	digest = sha256(f"{company}\0{legacy_identifier}".encode()).hexdigest()[:12].upper()
	return f"LEGACY-{digest}"


def _migrate_default_hospital() -> dict[str, object]:
	settings = cast(HRPSystemSettings, frappe.get_single("HRP System Settings"))
	legacy_identifier = settings.default_hospital
	if not legacy_identifier:
		return {"changed": False, "hospital": None}
	company = settings.default_company
	if not company or not frappe.db.exists("Company", company):
		raise_ione_error("CONFIGURATION_INVALID")

	if frappe.db.exists("HRP Hospital", legacy_identifier):
		hospital_company = frappe.db.get_value("HRP Hospital", legacy_identifier, "company")
		if hospital_company != company:
			raise_ione_error("CONFIGURATION_INVALID")
		return {"changed": False, "hospital": legacy_identifier}

	code = _legacy_hospital_code(company, legacy_identifier)
	hospital_created = False
	if frappe.db.exists("HRP Hospital", code):
		hospital = cast(HRPHospital, frappe.get_doc("HRP Hospital", code))
		if hospital.company != company or hospital.display_name != legacy_identifier:
			raise_ione_error("CONFIGURATION_INVALID")
	else:
		hospital = cast(
			HRPHospital,
			frappe.get_doc(
				{
					"doctype": "HRP Hospital",
					"code": code,
					"display_name": legacy_identifier,
					"company": company,
					"enabled": 1,
					"revision": 1,
					"next_version_number": 1,
					"remarks": "由系统设置旧版默认医院标识迁移",
				}
			),
		)
		hospital.flags.organization_migration = True
		hospital.insert(ignore_permissions=True)
		hospital_created = True

	settings.default_hospital = code
	settings.flags.system_settings_repair = True
	settings.save(ignore_permissions=True)
	emit_audit_event(
		"default_hospital_migrated",
		logger_name="medops.organization",
		configuration_version=settings.configuration_version,
		hospital_created=hospital_created,
	)
	return {"changed": True, "hospital": code}


def ensure_organization_hierarchy() -> dict[str, object]:
	for doctype, fields, constraint_name in (
		(
			"HRP Organization Version",
			["hospital", "effective_from"],
			"uniq_hrp_org_version_hospital_date",
		),
		(
			"HRP Organization Unit",
			["organization_version", "code"],
			"uniq_hrp_org_unit_version_code",
		),
		(
			"HRP Organization Mapping",
			["organization_version", "organization_unit"],
			"uniq_hrp_org_mapping_version_unit",
		),
		(
			"HRP Organization Mapping",
			["organization_version", "department"],
			"uniq_hrp_org_mapping_version_department",
		),
		(
			"HRP Organization Mapping",
			["organization_version", "cost_center"],
			"uniq_hrp_org_mapping_version_cost_center",
		),
	):
		if frappe.db.table_exists(doctype):
			frappe.db.add_unique(doctype, fields, constraint_name)
	migration = _migrate_default_hospital()
	return {
		"schema_version": 2,
		"default_hospital_changed": migration["changed"],
		"default_hospital": migration["hospital"],
	}


__all__ = ["ensure_organization_hierarchy"]
