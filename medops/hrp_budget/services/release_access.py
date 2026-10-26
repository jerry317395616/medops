from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

import frappe

from medops.common.budget_ledger import RELEASE, RELEASE_EVENT_PUBLIC_FIELDS, RELEASE_PUBLIC_FIELDS
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services.access_graph import read_graph
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.release_evidence import validate_release_evidence
from medops.hrp_budget.services.release_sources import approved_release_source, stored_release_facts
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.data_access import scope_allowed, scope_permission_query
from medops.services.errors import raise_ione_error

RELEASE_READ_ROLES = MANAGER_ROLES | {"HRP Auditor"}
_query: ContextVar[bool] = ContextVar("medops_budget_release_query", default=False)


@contextmanager
def release_query_scope():
	token = _query.set(True)
	try:
		yield
	finally:
		_query.reset(token)


def release_query(user=None):
	user = user or frappe.session.user
	return "" if user == "Administrator" else scope_permission_query(RELEASE, user) if _query.get() else "1=0"


def event_query(user=None):
	return "" if (user or frappe.session.user) == "Administrator" else "1=0"


def ledger_query(user=None):
	return "" if (user or frappe.session.user) == "Administrator" else "1=0"


def outbox_query(user=None):
	return "" if (user or frappe.session.user) == "Administrator" else "1=0"


@read_graph
def release_permission(doc, user=None, ptype=None, debug=False):
	del debug
	user = user or frappe.session.user
	if (
		user == "Guest"
		or ptype not in {None, "read", "select", "report", "export", "print"}
		or not RELEASE_READ_ROLES.intersection(frappe.get_roles(user))
	):
		return False
	if not doc.name or not frappe.db.exists(RELEASE, doc.name):
		return False
	doc = frappe.get_doc(RELEASE, doc.name)
	export = ptype in {"export", "print"}
	if not scope_allowed(doc, user=user, action="export" if export else "read") or not can_read_named_fields(
		doc, set(RELEASE_PUBLIC_FIELDS) - {"name"}, user=user
	):
		return False
	try:
		approved_release_source(doc.source_approval, user=user, export=export)
		stored_release_facts(doc)
	except (IoneApplicationError, frappe.DoesNotExistError):
		return False
	return True


@read_graph
def event_permission(doc, user=None, ptype=None, debug=False):
	del debug
	user = user or frappe.session.user
	if not doc.release or ptype not in {None, "read", "select", "report", "export", "print"}:
		return False
	if not frappe.db.exists(RELEASE, doc.release):
		return False
	parent = frappe.get_doc(RELEASE, doc.release)
	if not release_permission(parent, user=user, ptype=ptype):
		return False
	if (
		doc.event_kind == "Confirmation"
		and doc.actor != user
		and not {"System Manager", "HRP System Manager", "HRP Auditor"}.intersection(frappe.get_roles(user))
	):
		return False
	validate_release_evidence(doc)
	return scope_allowed(
		doc, user=user, action="export" if ptype in {"export", "print"} else "read"
	) and can_read_named_fields(doc, set(RELEASE_EVENT_PUBLIC_FIELDS) - {"name"}, user=user)


def ledger_permission(doc, user=None, ptype=None, debug=False):
	# Original-leaf statement service will own projection and private batch proof.
	# Generic native records cannot serialize private batch links or version digests.
	del doc, user, ptype, debug
	return False


@read_graph
def outbox_permission(doc, user=None, ptype=None, debug=False):
	del debug
	if (
		doc.reference_doctype != RELEASE
		or not doc.reference_name
		or not frappe.db.exists(RELEASE, doc.reference_name)
	):
		return False
	return release_permission(frappe.get_doc(RELEASE, doc.reference_name), user=user, ptype=ptype)


def get_release(name, *, lock=False, export=False):
	if not frappe.db.exists(RELEASE, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	doc = frappe.get_doc(RELEASE, name, for_update=lock)
	ptype = "export" if export else "read"
	if not frappe.has_permission(RELEASE, ptype, doc=doc) or not release_permission(doc, ptype=ptype):
		raise_ione_error("PERMISSION_DENIED")
	return doc
