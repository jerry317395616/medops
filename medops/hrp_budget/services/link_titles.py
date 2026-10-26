from __future__ import annotations

from typing import Any

import frappe
from frappe.desk.search import get_link_title as native_link_title

from medops.common.budget_approval import SUBMISSION
from medops.common.budget_planning import MASTER_TYPES, PLAN, VERSION
from medops.common.budget_workflow import CASE, REVIEW
from medops.common.organization import normalize_reference
from medops.hrp_budget.services.planning_access import get_plan, get_version
from medops.hrp_budget.services.submission_access import get_submission
from medops.hrp_master_data.services.budget_dimensions import get_budget_master
from medops.services.errors import raise_ione_error, require_authenticated_user


@frappe.whitelist(methods=["GET", "POST"])
def get_link_title(doctype: Any = None, docname: Any = None):
	"""Guard only owned planning titles; preserve other native title semantics."""
	require_authenticated_user()
	try:
		doctype = normalize_reference(doctype, label="doctype")
		if doctype not in {*MASTER_TYPES.values(), VERSION, PLAN, SUBMISSION, CASE, REVIEW}:
			return native_link_title(doctype, docname)
		name = normalize_reference(docname, label="docname")
	except ValueError as exc:
		raise_ione_error("INVALID_REQUEST", cause=exc)
	if doctype == VERSION:
		doc = get_version(name)
	elif doctype == PLAN:
		doc = get_plan(name)
	elif doctype == SUBMISSION:
		doc = get_submission(name)
	elif doctype == CASE:
		from medops.hrp_budget.services.approval_access import get_case

		doc = get_case(name)
	elif doctype == REVIEW:
		from medops.hrp_budget.services.approval_access import review_permission

		if not frappe.db.exists(REVIEW, name):
			raise_ione_error("RESOURCE_NOT_FOUND")
		doc = frappe.get_doc(REVIEW, name)
		if not frappe.has_permission(REVIEW, "read", doc=doc) or not review_permission(doc, ptype="read"):
			raise_ione_error("PERMISSION_DENIED")
		return doc.name
	else:
		doc = get_budget_master(doctype, name)
	return doc.get(doc.meta.title_field)
