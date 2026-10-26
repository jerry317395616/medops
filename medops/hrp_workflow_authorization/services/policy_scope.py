from __future__ import annotations

import frappe

from medops.services.errors import raise_ione_error


def assert_policy_tree_integrity(
	*, policy_doctype, target_doctype, company, hospital, effective_on, action=None
):
	"""Unknown policy ancestry must fail closed before excluding unrelated units."""
	if policy_doctype not in {"HRP Approval Matrix", "HRP Segregation Rule"}:
		raise_ione_error("CONFIGURATION_INVALID")
	where = " AND policy.target_action=%s" if policy_doctype == "HRP Segregation Rule" else ""
	values = [target_doctype, company, hospital, effective_on, effective_on]
	if where:
		values.append(action)
	rows = frappe.db.sql(
		f"""SELECT policy.name FROM `tab{policy_doctype}` policy
		LEFT JOIN `tabHRP Organization Unit` root ON root.name=policy.organization_unit
		WHERE policy.target_doctype=%s AND policy.company=%s AND policy.hospital=%s
		AND policy.enabled=1 AND policy.valid_from<=%s
		AND (policy.valid_to IS NULL OR policy.valid_to>=%s){where}
		AND COALESCE(policy.organization_unit,'')<>''
		AND (root.name IS NULL OR root.company<>policy.company OR root.hospital<>policy.hospital
		OR root.enabled<>1 OR (policy.include_descendants=1
		AND (root.lft IS NULL OR root.rgt IS NULL OR root.lft<1 OR root.rgt<=root.lft)))
		LIMIT 1""",
		values,
	)
	if rows:
		raise_ione_error("CONFIGURATION_INVALID")
