from __future__ import annotations

import frappe


def ensure_budget_submission():
	# Definitions and indexes only. No users, scopes, shares or financial seeds.
	for doctype, fields, name in (
		("HRP Budget Submission", ["source_plan"], "uniq_hrp_submission_plan"),
		(
			"HRP Budget Submission",
			["company", "hospital", "organization_unit", "fiscal_year"],
			"idx_hrp_submission_scope",
		),
		("HRP Budget Submission", ["source_version", "creation"], "idx_hrp_submission_version"),
		(
			"HRP Budget Submission",
			["company", "hospital", "submission_type", "fiscal_year"],
			"idx_hrp_submission_kind",
		),
		("HRP Budget Submission Line", ["origin_plan", "parent"], "idx_hrp_submission_origin"),
		(
			"HRP Budget Submission Line",
			["parent", "parenttype", "parentfield", "fact_key"],
			"uniq_hrp_submission_fact",
		),
	):
		if not frappe.db.table_exists(doctype):
			continue
		if name.startswith("uniq_"):
			frappe.db.add_unique(doctype, fields, name)
		else:
			frappe.db.add_index(doctype, fields, name)
	return {"schema_version": 1}
