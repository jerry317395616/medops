from __future__ import annotations

import frappe

from medops.hrp_master_data.services.quality_history import ensure_quality_history


def ensure_core_reports():
	for doctype, fields, name in (
		(
			"HRP Reference Link",
			["company", "hospital", "organization_unit", "master_data_domain", "enabled"],
			"idx_hrp_reference_population",
		),
		("HRP Data Quality Event", ["issue", "occurred_at", "issue_revision"], "idx_hrp_quality_event_issue"),
		(
			"HRP Data Quality Event",
			["company", "hospital", "organization_unit", "occurred_at"],
			"idx_hrp_quality_event_scope",
		),
	):
		if frappe.db.table_exists(doctype):
			frappe.db.add_index(doctype, fields, name)
	return ensure_quality_history()
