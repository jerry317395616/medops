from __future__ import annotations

import frappe


def ensure_segregation_governance() -> dict[str, object]:
	for fields, index_name in (
		(
			["target_doctype", "target_action", "company", "hospital", "enabled", "valid_from", "valid_to"],
			"idx_hrp_segregation_rule_resolution",
		),
		(
			["organization_unit", "include_descendants"],
			"idx_hrp_segregation_rule_organization",
		),
	):
		if frappe.db.table_exists("HRP Segregation Rule"):
			frappe.db.add_index("HRP Segregation Rule", fields, index_name)
	return {"schema_version": 1}


__all__ = ["ensure_segregation_governance"]
