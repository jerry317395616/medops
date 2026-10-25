from __future__ import annotations

import frappe


def ensure_delegation_governance() -> dict[str, object]:
	for fields, index_name in (
		(
			[
				"matrix",
				"matrix_revision",
				"matrix_digest",
				"status",
				"valid_from",
				"valid_to",
			],
			"idx_hrp_delegation_resolution",
		),
		(
			["from_user", "to_user", "status", "valid_from", "valid_to", "step_sequence"],
			"idx_hrp_delegation_principals",
		),
	):
		if frappe.db.table_exists("HRP Delegation"):
			frappe.db.add_index("HRP Delegation", fields, index_name)
	return {"schema_version": 1}


__all__ = ["ensure_delegation_governance"]
