from __future__ import annotations

import frappe


def ensure_budget_planning():
	# Definitions only: no existing user receives any role or data scope.
	for name in ("HRP Budget User", "HRP Budget Manager", "HRP Finance User"):
		if not frappe.db.exists("Role", name):
			frappe.get_doc({"doctype": "Role", "role_name": name, "desk_access": 1, "is_custom": 0}).insert(
				ignore_permissions=True
			)
	for doctype, fields, name in (
		(
			"HRP Budget Version",
			["hospital", "fiscal_year", "version_number"],
			"uniq_hrp_budget_version_number",
		),
		(
			"HRP Budget Version",
			["company", "hospital", "fiscal_year", "status"],
			"idx_hrp_budget_version_scope",
		),
		("HRP Budget Plan", ["budget_version", "organization_unit"], "uniq_hrp_budget_plan_unit"),
		(
			"HRP Budget Plan",
			["company", "hospital", "organization_unit", "status"],
			"idx_hrp_budget_plan_scope",
		),
		(
			"HRP Budget Plan Line",
			["parent", "parenttype", "parentfield", "line_key"],
			"uniq_hrp_budget_plan_line",
		),
		("HRP Budget Plan Line", ["account"], "idx_hrp_budget_line_account"),
		("HRP Budget Plan Line", ["project"], "idx_hrp_budget_line_project"),
		(
			"HRP Budget Plan Line",
			["budget_subject", "funding_source", "period_index"],
			"idx_hrp_budget_line_dimension",
		),
		(
			"HRP Business Dimension",
			["company", "hospital", "purpose", "enabled"],
			"idx_hrp_business_dimension_scope",
		),
		(
			"HRP Dimension Value",
			["company", "hospital", "dimension", "enabled"],
			"idx_hrp_dimension_value_scope",
		),
		("HRP Funding Source", ["company", "hospital", "enabled"], "idx_hrp_funding_source_scope"),
	):
		if not frappe.db.table_exists(doctype):
			continue
		if name.startswith("uniq_"):
			frappe.db.add_unique(doctype, fields, name)
		else:
			frappe.db.add_index(doctype, fields, name)
	return {"schema_version": 1}
