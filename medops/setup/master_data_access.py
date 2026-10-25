from __future__ import annotations

import frappe

from medops.common.domain_service import canonical_json
from medops.common.master_data import MASTER_DATA_TARGET_POLICIES


def ensure_master_data_access_contracts() -> dict[str, object]:
	"""Conservatively protect legacy verdict history; never invent the field it evaluated."""
	doctype = "HRP Data Quality Issue"
	if not frappe.db.table_exists(doctype) or not frappe.db.has_column(doctype, "evaluation_read_fields"):
		return {"updated": 0}
	after = ""
	updated = 0
	while True:
		rows = frappe.get_all(
			doctype,
			filters={"name": (">", after)},
			fields=["name"],
			order_by="name asc",
			limit_page_length=200,
		)
		if not rows:
			break
		for row in rows:
			doc = frappe.get_doc(doctype, row.name, for_update=True)
			if doc.get("evaluation_read_fields"):
				continue
			policy = MASTER_DATA_TARGET_POLICIES.get(doc.target_doctype)
			if policy is None:
				continue
			doc.evaluation_read_fields = canonical_json(sorted(policy.fields_by_name))
			doc.flags.master_data_migration = True
			doc.flags.locked_revision = int(doc.revision)
			doc.save(ignore_permissions=True)
			updated += 1
		after = rows[-1].name
	return {"updated": updated}
