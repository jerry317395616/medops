from __future__ import annotations

import frappe


def ensure_access_scope_governance() -> dict[str, object]:
	for doctype, fields, index_name in (
		(
			"HRP Access Scope",
			["enabled", "valid_from", "valid_to"],
			"idx_hrp_access_scope_effectivity",
		),
		(
			"HRP Access Scope",
			["company", "hospital", "organization_unit"],
			"idx_hrp_access_scope_organization",
		),
		(
			"HRP Access Scope Member",
			["user", "target_doctype", "enabled"],
			"idx_hrp_access_scope_member_user",
		),
	):
		if frappe.db.table_exists(doctype):
			frappe.db.add_index(doctype, fields, index_name)
	return {"schema_version": 1}


__all__ = ["ensure_access_scope_governance"]
