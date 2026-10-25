from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import frappe

from medops.common.core_reports import MAX_REPORT_RECORDS, QUALITY_EVENT, REFERENCE
from medops.hrp_master_data.services.access import get_scoped_documents, master_data_visible
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.data_access import scope_allowed, scope_permission_query
from medops.services.errors import raise_ione_error

_query: ContextVar[bool] = ContextVar("medops_core_report_query", default=False)


@contextmanager
def scoped_query():
	token = _query.set(True)
	try:
		yield
	finally:
		_query.reset(token)


def scoped_query_active():
	return _query.get()


def require_fields(doc, fields, *, export=False):
	if (
		not frappe.has_permission(doc.doctype, "read", doc=doc)
		or not scope_allowed(doc, action="export" if export else "read")
		or not can_read_named_fields(doc, fields)
	):
		raise_ione_error("PERMISSION_DENIED")
	if export and not frappe.has_permission(doc.doctype, "export", doc=doc):
		raise_ione_error("PERMISSION_DENIED")


def visible_documents(doctype: str, filters: dict[str, Any], *, export=False):
	allowed = {
		REFERENCE,
		"HRP Organization Mapping",
		"HRP Delegation",
		"HRP Master Data Request",
		"HRP External Code Mapping",
		"HRP Data Quality Rule",
		"HRP Data Quality Issue",
		"HRP SLA Escalation",
		"ToDo",
	}
	if doctype not in allowed:
		raise_ione_error("INVALID_REQUEST")
	if not frappe.has_permission(doctype, "read"):
		return []
	if doctype.startswith("HRP Master Data") or doctype in {
		"HRP External Code Mapping",
		"HRP Data Quality Rule",
		"HRP Data Quality Issue",
	}:
		docs = get_scoped_documents(doctype, filters=filters)
	else:
		with scoped_query():
			rows = frappe.get_list(
				doctype,
				fields=["name"],
				filters=filters,
				order_by="modified desc, name desc",
				limit=MAX_REPORT_RECORDS + 1,
			)
		if len(rows) > MAX_REPORT_RECORDS:
			raise_ione_error("INVALID_REQUEST")
		docs = [frappe.get_doc(doctype, row.name) for row in rows]
	result = []
	for doc in docs:
		if not frappe.has_permission(doctype, "read", doc=doc) or not scope_allowed(
			doc, action="export" if export else "read"
		):
			continue
		if export and not frappe.has_permission(doctype, "export", doc=doc):
			continue
		if doctype in {
			"HRP Master Data Request",
			"HRP External Code Mapping",
			"HRP Data Quality Rule",
			"HRP Data Quality Issue",
		} and not master_data_visible(doc, action="export" if export else "read"):
			continue
		result.append(doc)
	return result


def history_query(user=None):
	user = user or frappe.session.user
	return (
		scope_permission_query(QUALITY_EVENT, user)
		if scoped_query_active() or user == "Administrator"
		else "1=0"
	)


def history_permission(doc, user=None, ptype=None, debug=False):
	del debug
	if not doc.issue or ptype not in {None, "read", "select", "report", "export", "print"}:
		return False
	user = user or frappe.session.user
	if not frappe.db.exists("HRP Data Quality Issue", doc.issue):
		return False
	issue = frappe.get_doc("HRP Data Quality Issue", doc.issue)
	return (
		frappe.has_permission(issue.doctype, "read", doc=issue, user=user)
		and master_data_visible(issue, user=user, action="export" if ptype in {"export", "print"} else "read")
		and scope_allowed(doc, user=user, action="export" if ptype in {"export", "print"} else "read")
	)
