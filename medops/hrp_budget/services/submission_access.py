from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

import frappe

from medops.common.budget_approval import SUBMISSION, SUBMISSION_LINE, department_snapshot_from_payload
from medops.common.budget_planning import PLAN, VERSION, plan_definition, planning_digest
from medops.hrp_budget.services.planning_access import READ_ROLES, native_source, plan_permission
from medops.hrp_budget.services.submission_documents import stored_facts
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.data_access import scope_allowed, scope_permission_query
from medops.services.errors import raise_ione_error

_query: ContextVar[bool] = ContextVar("medops_budget_submission_query", default=False)


@contextmanager
def submission_query_scope():
	token = _query.set(True)
	try:
		yield
	finally:
		_query.reset(token)


def submission_query(user=None):
	user = user or frappe.session.user
	return (
		"" if user == "Administrator" else scope_permission_query(SUBMISSION, user) if _query.get() else "1=0"
	)


def submission_line_query(user=None):
	return "" if (user or frappe.session.user) == "Administrator" else "1=0"


def submission_permission(doc, user=None, ptype=None, debug=False):
	del debug
	user = user or frappe.session.user
	if user == "Guest" or ptype not in {None, "read", "select", "report", "export", "print"}:
		return False
	export = ptype in {"export", "print"}
	if not READ_ROLES.intersection(frappe.get_roles(user)):
		return False
	# Native getdoc calls controller permissions again after stripping higher
	# field levels. Reload only for verification; never restore private values
	# onto the redacted document that Frappe is about to return.
	if doc.get("source_version_digest") is None:
		if not doc.name or not frappe.db.exists(SUBMISSION, doc.name):
			return False
		doc = frappe.get_doc(SUBMISSION, doc.name)
	if not scope_allowed(doc, user=user, action="export" if export else "read"):
		return False
	named = {field.fieldname for field in doc.meta.fields} - {"source_version_digest"}
	named |= {
		"lines." + field.fieldname
		for field in frappe.get_meta(SUBMISSION_LINE).fields
		if field.fieldname != "origin_version_digest"
	}
	if not can_read_named_fields(doc, named, user=user):
		return False
	# A snapshot never grants access to its source. The complete plan permission
	# includes every linked master and native source field; do not filter rows.
	plan = native_source(PLAN, doc.source_plan, {"name"}, user=user, export=export)
	if plan is None or not plan_permission(plan, user=user, ptype="export" if export else "read"):
		return False
	version = native_source(
		VERSION, doc.source_version, {"status", "currency_precision"}, user=user, export=export
	)
	if version is None:
		return False
	if (
		version.docstatus != 1
		or version.status != "Frozen"
		or plan.status != "Draft"
		or plan.budget_version != version.name
		or plan.name != doc.source_plan
		or plan.organization_unit != doc.organization_unit
		or plan.cost_center != doc.cost_center
		or int(plan.revision) != int(doc.source_plan_revision)
		or version.snapshot_digest != doc.source_version_digest
		or planning_digest(plan_definition(plan.as_public_dict())) != doc.source_plan_digest
	):
		return False
	if not all(
		str(plan.get(key)) == str(doc.get(key)) and str(version.get(key)) == str(doc.get(key))
		for key in (
			"company",
			"hospital",
			"organization_version",
			"fiscal_year",
			"currency",
			"currency_precision",
		)
	):
		return False
	try:
		department_snapshot_from_payload(stored_facts(doc))
	except (ValueError, TypeError, KeyError):
		return False
	return True


def submission_line_permission(doc, user=None, ptype=None, debug=False):
	del debug
	if doc.parenttype != SUBMISSION or not doc.parent or not frappe.db.exists(SUBMISSION, doc.parent):
		return False
	parent = frappe.get_doc(SUBMISSION, doc.parent)
	return frappe.has_permission(SUBMISSION, "read", doc=parent, user=user) and submission_permission(
		parent, user=user, ptype=ptype
	)


def get_submission(name: str, *, lock=False):
	if not frappe.db.exists(SUBMISSION, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	doc = frappe.get_doc(SUBMISSION, name, for_update=lock)
	if not frappe.has_permission(SUBMISSION, "read", doc=doc) or not submission_permission(doc, ptype="read"):
		raise_ione_error("PERMISSION_DENIED")
	return doc
