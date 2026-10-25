from __future__ import annotations

import frappe


def ensure_master_data_governance() -> dict[str, object]:
	for doctype, fields, constraint_name in (
		(
			"HRP Master Data Domain",
			["target_doctype"],
			"uniq_hrp_master_data_domain_target",
		),
		(
			"HRP Master Data Request",
			["master_data_domain", "target_name", "request_status"],
			"idx_hrp_master_data_request_target_status",
		),
		(
			"HRP External Code Mapping",
			["source_key"],
			"uniq_hrp_external_code_source",
		),
		(
			"HRP External Code Mapping",
			["target_key"],
			"uniq_hrp_external_code_target",
		),
		(
			"HRP External Code Mapping",
			["enabled", "valid_from", "valid_to"],
			"idx_hrp_external_code_effectivity",
		),
		(
			"HRP Data Quality Rule",
			["enabled", "valid_from", "valid_to"],
			"idx_hrp_data_quality_rule_schedule",
		),
		(
			"HRP Data Quality Issue",
			["issue_key"],
			"uniq_hrp_data_quality_issue_key",
		),
		(
			"HRP Data Quality Issue",
			["issue_status", "severity", "last_evaluated_at"],
			"idx_hrp_data_quality_issue_status",
		),
	):
		if not frappe.db.table_exists(doctype):
			continue
		if constraint_name.startswith("uniq_"):
			frappe.db.add_unique(doctype, fields, constraint_name)
		else:
			frappe.db.add_index(doctype, fields, constraint_name)
	return {"schema_version": 1}


__all__ = ["ensure_master_data_governance"]
