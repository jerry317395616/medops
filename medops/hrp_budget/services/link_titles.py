from __future__ import annotations

from typing import Any

import frappe
from frappe.desk.search import get_link_title as native_link_title

from medops.common.budget_approval import SUBMISSION
from medops.common.budget_ledger import LEDGER, RELEASE, RELEASE_ENTRY
from medops.common.budget_planning import MASTER_TYPES, PLAN, VERSION
from medops.common.budget_workflow import CASE, REVIEW
from medops.common.integration_outbox import OUTBOX, OUTBOX_DEFINITION
from medops.common.organization import normalize_reference
from medops.hrp_budget.services.planning_access import get_plan, get_version
from medops.hrp_budget.services.submission_access import get_submission
from medops.hrp_master_data.services.budget_dimensions import get_budget_master
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.errors import raise_ione_error, require_authenticated_user


@frappe.whitelist(methods=["GET", "POST"])
def get_link_title(doctype: Any = None, docname: Any = None):
	"""Guard only owned planning titles; preserve other native title semantics."""
	require_authenticated_user()
	try:
		doctype = normalize_reference(doctype, label="doctype")
		if doctype not in {
			*MASTER_TYPES.values(),
			VERSION,
			PLAN,
			SUBMISSION,
			CASE,
			REVIEW,
			LEDGER,
			RELEASE,
			RELEASE_ENTRY,
			OUTBOX,
		}:
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
	elif doctype == RELEASE:
		from medops.hrp_budget.services.release_access import get_release

		doc = get_release(name)
	elif doctype == RELEASE_ENTRY:
		from medops.hrp_budget.services.release_access import event_permission

		if not frappe.db.exists(RELEASE_ENTRY, name):
			raise_ione_error("RESOURCE_NOT_FOUND")
		doc = frappe.get_doc(RELEASE_ENTRY, name)
		if not frappe.has_permission(RELEASE_ENTRY, "read", doc=doc) or not event_permission(
			doc, ptype="read"
		):
			raise_ione_error("PERMISSION_DENIED")
		return doc.name
	elif doctype == LEDGER:
		# Original-leaf statement access must be complete before enabling titles.
		raise_ione_error("PERMISSION_DENIED")
	elif doctype == OUTBOX:
		from medops.hrp_budget.services.release_access import outbox_permission
		from medops.services.transactional_message import read_message_document

		doc = read_message_document(OUTBOX_DEFINITION, name)
		if (
			not frappe.has_permission(OUTBOX, "read", doc=doc)
			or not outbox_permission(doc, ptype="read")
			or not can_read_named_fields(doc, {"event_id"})
		):
			raise_ione_error("PERMISSION_DENIED")
		return doc.event_id
	else:
		doc = get_budget_master(doctype, name)
	return doc.get(doc.meta.title_field)
