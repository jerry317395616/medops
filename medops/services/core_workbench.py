from __future__ import annotations

import json

import frappe

from medops.common.core_reports import REPORTS, report_filters
from medops.hrp_master_data.services.access import master_data_visible, scoped_list_query
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.audit_context import emit_audit_event
from medops.services.core_report_access import scoped_query
from medops.services.data_access import scope_allowed
from medops.services.errors import raise_ione_error, require_roles

ROLES = frozenset(
	{
		"System Manager",
		"HRP System Manager",
		"HRP Data Steward",
		"HRP User",
		"HRP Auditor",
		"HRP Department Manager",
		"HRP Integration User",
	}
)


def source_visible(doctype, name, fields=()):
	if (
		not doctype
		or not name
		or not frappe.db.exists("DocType", doctype)
		or not frappe.db.exists(doctype, name)
	):
		return False
	doc = frappe.get_doc(doctype, name)
	return (
		frappe.has_permission(doctype, "read", doc=doc)
		and scope_allowed(doc)
		and can_read_named_fields(doc, fields)
	)


def preview(doctype, filters, fields, visible):
	if not frappe.has_permission(doctype, "read"):
		return {"items": [], "has_more": False, "available": False}
	with scoped_list_query(), scoped_query():
		candidates = frappe.get_list(
			doctype, fields=["name"], filters=filters, order_by="modified desc,name desc", limit=101
		)
	items = []
	for row in candidates[:100]:
		doc = frappe.get_doc(doctype, row.name)
		if (
			not frappe.has_permission(doctype, "read", doc=doc)
			or not can_read_named_fields(doc, fields)
			or not visible(doc)
		):
			continue
		items.append(
			{
				key: str(doc.get(key)) if key.endswith("_at") or key in {"date", "modified"} else doc.get(key)
				for key in fields
			}
		)
		if len(items) == 10:
			break
	return {"items": items, "has_more": len(candidates) > 100 or len(items) == 10, "available": True}


def workbench(filters=None):
	require_roles(ROLES)
	try:
		context = report_filters(filters)
		if set(context) - {"company", "hospital", "organization_unit"}:
			raise ValueError("unknown workbench filter")
	except (ValueError, TypeError) as exc:
		raise_ione_error("INVALID_REQUEST", cause=exc)
	sections = {}
	sections["requests"] = preview(
		"HRP Master Data Request",
		{**context, "request_status": "Pending Review"},
		{"name", "subject", "request_status", "submitted_at"},
		lambda doc: scope_allowed(doc) and master_data_visible(doc),
	)
	sections["issues"] = preview(
		"HRP Data Quality Issue",
		{**context, "issue_status": "Open"},
		{"name", "target_doctype", "target_name", "severity", "last_evaluated_at"},
		lambda doc: scope_allowed(doc) and master_data_visible(doc),
	)

	def todo_visible(doc):
		if not doc.reference_type or not doc.reference_name:
			return not context
		if not source_visible(doc.reference_type, doc.reference_name):
			return False
		source = frappe.get_doc(doc.reference_type, doc.reference_name)
		return not any(source.get(k) != v for k, v in context.items())

	# Free-text ToDo descriptions can contain copied private source values; never return them.
	sections["todos"] = preview(
		"ToDo",
		{"allocated_to": frappe.session.user, "status": "Open"},
		{"name", "reference_type", "reference_name", "date", "priority"},
		todo_visible,
	)

	def sla_visible(doc):
		try:
			snapshot = json.loads(doc.snapshot_json or "{}")
		except ValueError:
			return False
		if not isinstance(snapshot, dict) or not isinstance(snapshot.get("policy"), dict):
			return False
		definition = snapshot["policy"]
		fields = {"company", "hospital", "organization_unit", "docstatus"}
		for key in ("status_field", "start_field", "response_field"):
			if definition.get(key):
				fields.add(definition[key])
		if not frappe.db.exists(doc.reference_doctype, doc.reference_name):
			return False
		source = frappe.get_doc(doc.reference_doctype, doc.reference_name)
		fields = {field for field in fields if source.meta.has_field(field) or field == "docstatus"}
		return scope_allowed(doc) and source_visible(doc.reference_doctype, doc.reference_name, fields)

	sections["sla"] = preview(
		"HRP SLA Escalation",
		{**context, "status": ("in", ["Open", "Paused"])},
		{
			"name",
			"reference_doctype",
			"reference_name",
			"status",
			"response_due_at",
			"resolution_due_at",
			"response_breached",
			"resolution_breached",
		},
		sla_visible,
	)
	reports = []
	for code, (name, label, _module, source) in REPORTS.items():
		if (
			frappe.db.exists("Report", name)
			and frappe.get_doc("Report", name).is_permitted()
			and frappe.has_permission(source, "report")
		):
			reports.append({"code": code, "name": name, "label": label})
	emit_audit_event(
		"core_workbench_read",
		logger_name="medops.workbench",
		returned=sum(len(section["items"]) for section in sections.values()),
	)
	return {"sections": sections, "reports": reports, "counts_are_preview_only": True}
