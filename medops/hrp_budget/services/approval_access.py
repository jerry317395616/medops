from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

import frappe

from medops.common.budget_workflow import APPROVER_ROLE, CASE, REVIEW, approval_mutation, parse_decision
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.hrp_budget.services.access_graph import read_graph
from medops.hrp_budget.services.planning_access import READ_ROLES
from medops.hrp_budget.services.statement_cache import cached_statement_value, statement_document_key
from medops.hrp_budget.services.submission_access import submission_permission
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.data_access import scope_allowed, scope_permission_query
from medops.services.errors import raise_ione_error

APPROVE_ROLES = frozenset({APPROVER_ROLE, "System Manager", "HRP System Manager"})
_query: ContextVar[bool] = ContextVar("medops_budget_case_query", default=False)


@contextmanager
def case_query_scope():
	token = _query.set(True)
	try:
		yield
	finally:
		_query.reset(token)


def case_query(user=None):
	user = user or frappe.session.user
	return "" if user == "Administrator" else scope_permission_query(CASE, user) if _query.get() else "1=0"


def review_query(user=None):
	return "" if (user or frappe.session.user) == "Administrator" else "1=0"


def forbidden_actors(doc) -> set[str]:
	actors = {str(doc.get(key)) for key in ("applicant", "source_applicant", "requested_by") if doc.get(key)}
	if doc.submission and frappe.db.exists("HRP Budget Submission", doc.submission):
		source = frappe.get_doc("HRP Budget Submission", doc.submission)
		if source.submission_type == "Consolidated":
			from medops.common.budget_consolidation import source_references

			for row in source_references(source.sources_snapshot):
				actors.update((row.preparer, row.applicant, row.requested_by))
	return actors


def submission_preparer(source):
	return (
		source.applicant
		if source.submission_type == "Consolidated"
		else frappe.db.get_value("HRP Budget Plan", source.source_plan, "applicant")
	)


def approver_role_active(user: str) -> bool:
	state = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
	return bool(
		state
		and state.enabled
		and state.user_type == "System User"
		and APPROVE_ROLES.intersection(frappe.get_roles(user))
	)


def case_source(doc, user: str, *, export=False):
	if not doc.submission or not frappe.db.exists("HRP Budget Submission", doc.submission):
		return None
	source = frappe.get_doc("HRP Budget Submission", doc.submission)
	if (
		not frappe.has_permission(source.doctype, "read", doc=source, user=user)
		or (export and not frappe.has_permission(source.doctype, "export", doc=source, user=user))
		or not submission_permission(source, user=user, ptype="export" if export else "read")
	):
		return None
	if source.docstatus != 1 or source.status != "Sealed" or source.snapshot_digest != doc.source_digest:
		return None
	for key in (
		"company",
		"hospital",
		"organization_version",
		"organization_unit",
		"cost_center",
		"fiscal_year",
		"currency",
		"currency_precision",
		"total_amount",
		"subject",
		"applicant",
	):
		if str(source.get(key)) != str(doc.get(key)):
			return None
	if submission_preparer(source) != doc.source_applicant:
		return None
	return source


@read_graph
def case_permission(doc, user=None, ptype=None, debug=False):
	del debug
	user = user or frappe.session.user
	# Bind every supplied field, including redaction, so a different in-memory
	# case cannot reuse an earlier complete-source permission for the same name.
	return cached_statement_value(
		lambda: ("case_permission", statement_document_key(doc), user, ptype),
		lambda: _case_permission(doc, user, ptype),
	)


def _case_permission(doc, user, ptype):
	if (
		user == "Guest"
		or ptype not in {None, "read", "select", "report", "export", "print"}
		or not READ_ROLES.intersection(frappe.get_roles(user))
	):
		return False
	from medops.hrp_budget.services.approval_native import require_workflow_integrity

	# Cache only successful integrity, never a mutable Workflow controller. The
	# final read phase validates the definition again; commands still read/lock.
	if approval_mutation():
		require_workflow_integrity()
	else:
		cached_statement_value(("workflow_integrity",), lambda: bool(require_workflow_integrity()))
	exists = bool(doc.name and frappe.db.exists(CASE, doc.name))
	if exists and doc.get("decision_snapshot") is None:
		doc = frappe.get_doc(CASE, doc.name)
	if not exists:
		mutation = approval_mutation()
		if not mutation or mutation.operation != "open" or mutation.case != doc.name:
			return False
	if not scope_allowed(doc, user=user, action="export" if ptype in {"export", "print"} else "read"):
		return False
	if (
		not can_read_named_fields(
			doc, {f.fieldname for f in doc.meta.fields} - {"decision_snapshot"}, user=user
		)
		or case_source(doc, user, export=ptype in {"export", "print"}) is None
	):
		return False
	if exists:
		try:
			decision = parse_decision(doc.decision_snapshot)
			if (
				decision["docname"] != doc.name
				or decision["matrix"] != doc.policy_matrix
				or decision["policy_version"] != int(doc.policy_revision)
				or decision["policy_digest"] != doc.policy_digest
				or decision["decision_digest"] != doc.decision_digest
				or decision["amount_precision"] != int(doc.currency_precision)
				or exact_decimal_amount(
					decision["amount"], int(doc.currency_precision), maximum=MAX_APPROVAL_AMOUNT
				)
				!= doc.total_amount
			):
				return False
		except (ValueError, TypeError, KeyError):
			return False
	return True


@read_graph
def can_approve_case(user: str, doc) -> bool:
	roles = set(frappe.get_roles(user))
	meta = frappe.get_meta(REVIEW)
	base = any(p.role in roles and p.read and not p.if_owner and not p.permlevel for p in meta.permissions)
	probe = frappe._dict(doctype=REVIEW, owner="")
	review_fields = can_read_named_fields(
		probe, {f.fieldname for f in meta.fields} - {"confirmation_hash", "session_digest"}, user=user
	)
	return bool(
		base
		and review_fields
		and approver_role_active(user)
		and user not in forbidden_actors(doc)
		and case_permission(doc, user=user, ptype="read")
		and scope_allowed(doc, user=user, action="write")
	)


@read_graph
def review_permission(doc, user=None, ptype=None, debug=False):
	del debug
	user = user or frappe.session.user
	if (
		ptype not in {None, "read", "select", "report", "export", "print"}
		or user == "Guest"
		or not doc.case
		or not frappe.db.exists(CASE, doc.case)
	):
		return False
	case = frappe.get_doc(CASE, doc.case)
	if not frappe.has_permission(CASE, "read", doc=case, user=user) or not case_permission(
		case, user=user, ptype=ptype
	):
		return False
	if (
		doc.event_kind == "Confirmation"
		and doc.actor != user
		and not {"System Manager", "HRP System Manager", "HRP Auditor"}.intersection(frappe.get_roles(user))
	):
		return False
	return (
		scope_allowed(doc, user=user, action="export" if ptype in {"export", "print"} else "read")
		and can_read_named_fields(
			doc, {f.fieldname for f in doc.meta.fields} - {"confirmation_hash", "session_digest"}, user=user
		)
		and all(doc.get(key) == case.get(key) for key in ("company", "hospital", "organization_unit"))
	)


@read_graph
def get_case(name: str, *, lock=False):
	if not frappe.db.exists(CASE, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	doc = frappe.get_doc(CASE, name, for_update=lock)
	if not frappe.has_permission(CASE, "read", doc=doc) or not case_permission(doc, ptype="read"):
		raise_ione_error("PERMISSION_DENIED")
	return doc
