from __future__ import annotations

from typing import Any

import frappe
from frappe.desk.search import get_link_title as native_link_title

from medops.common.budget_planning import MASTER_TYPES, PLAN, VERSION
from medops.common.organization import normalize_reference
from medops.hrp_budget.services.planning_access import get_plan, get_version
from medops.hrp_master_data.services.budget_dimensions import get_budget_master
from medops.services.errors import raise_ione_error, require_authenticated_user


@frappe.whitelist(methods=["GET", "POST"])
def get_link_title(doctype: Any = None, docname: Any = None):
	"""Guard only owned planning titles; preserve other native title semantics."""
	require_authenticated_user()
	try:
		doctype = normalize_reference(doctype, label="doctype")
		if doctype not in {*MASTER_TYPES.values(), VERSION, PLAN}:
			return native_link_title(doctype, docname)
		name = normalize_reference(docname, label="docname")
	except ValueError as exc:
		raise_ione_error("INVALID_REQUEST", cause=exc)
	if doctype == VERSION:
		doc = get_version(name)
	elif doctype == PLAN:
		doc = get_plan(name)
	else:
		doc = get_budget_master(doctype, name)
	return doc.get(doc.meta.title_field)
