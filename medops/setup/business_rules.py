from __future__ import annotations

import frappe


def ensure_business_rule_governance() -> dict[str, object]:
	for doctype, fields, name in (
		(
			"HRP Business Rule",
			["target_doctype", "target_action", "company", "hospital", "enabled"],
			"idx_hrp_business_rule_selection",
		),
		(
			"HRP Rule Execution Log",
			["company", "hospital", "organization_unit", "evaluated_at"],
			"idx_hrp_rule_log_scope",
		),
		(
			"HRP Rule Execution Log",
			["reference_doctype", "reference_name", "evaluated_at"],
			"idx_hrp_rule_log_source",
		),
	):
		if frappe.db.table_exists(doctype):
			frappe.db.add_index(doctype, fields, name)
	return {"schema_version": 1}
