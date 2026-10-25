from __future__ import annotations

import frappe


def ensure_sla_governance() -> dict[str, object]:
	for doctype, fields, name in (
		(
			"HRP SLA Policy",
			["target_doctype", "company", "hospital", "organization_unit", "enabled"],
			"idx_hrp_sla_policy_scope",
		),
		("HRP SLA Escalation", ["status", "next_run_at"], "idx_hrp_sla_due"),
		("HRP SLA Escalation", ["reference_doctype", "reference_name"], "idx_hrp_sla_source"),
	):
		if frappe.db.table_exists(doctype):
			frappe.db.add_index(doctype, fields, name)
	return {"schema_version": 1}
