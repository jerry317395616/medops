from __future__ import annotations

import frappe


def ensure_numbering_governance() -> dict[str, object]:
	for doctype, fields, constraint_name in (
		(
			"HRP Number Reservation",
			["number"],
			"uniq_hrp_number_reservation_number",
		),
		(
			"HRP Number Reservation",
			["reservation_token"],
			"uniq_hrp_number_reservation_token",
		),
		(
			"HRP Number Reservation",
			["numbering_scheme", "business_date", "creation"],
			"idx_hrp_number_reservation_scheme_date",
		),
		(
			"HRP Number Reservation",
			["allocated_by", "creation"],
			"idx_hrp_number_reservation_owner",
		),
		(
			"HRP Numbering Scheme",
			["enabled", "valid_from", "valid_to"],
			"idx_hrp_numbering_scheme_effectivity",
		),
	):
		if not frappe.db.table_exists(doctype):
			continue
		if constraint_name.startswith("uniq_"):
			frappe.db.add_unique(doctype, fields, constraint_name)
		else:
			frappe.db.add_index(doctype, fields, constraint_name)
	return {"schema_version": 1}


__all__ = ["ensure_numbering_governance"]
