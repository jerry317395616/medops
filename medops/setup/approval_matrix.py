from __future__ import annotations

import frappe


def ensure_approval_matrix_governance() -> dict[str, object]:
	for doctype, fields, index_name in (
		(
			"HRP Approval Matrix",
			["target_doctype", "company", "hospital", "enabled", "valid_from", "valid_to", "priority"],
			"idx_hrp_approval_matrix_resolution",
		),
		(
			"HRP Approval Matrix",
			["organization_unit", "include_descendants"],
			"idx_hrp_approval_matrix_organization",
		),
		(
			"HRP Approval Matrix Row",
			["parent", "parenttype", "parentfield", "sequence_no", "idx"],
			"idx_hrp_approval_matrix_step",
		),
	):
		if frappe.db.table_exists(doctype):
			frappe.db.add_index(doctype, fields, index_name)
	return {"schema_version": 1}


__all__ = ["ensure_approval_matrix_governance"]
