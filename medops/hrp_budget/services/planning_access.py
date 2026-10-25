from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import frappe

from medops.common.budget_planning import DIMENSION, FUND, PLAN, VALUE, VERSION
from medops.hrp_master_data.services.budget_dimensions import native_read
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.data_access import require_scope, scope_allowed, scope_permission_query
from medops.services.errors import raise_ione_error

MANAGER_ROLES = frozenset({"System Manager", "HRP System Manager", "HRP Budget Manager"})
WRITE_ROLES = MANAGER_ROLES | {"HRP Budget User", "HRP Finance User"}
READ_ROLES = WRITE_ROLES | {"HRP Auditor"}
_query: ContextVar[bool] = ContextVar("medops_budget_query", default=False)


@contextmanager
def planning_query_scope():
	token = _query.set(True)
	try:
		yield
	finally:
		_query.reset(token)


def version_query(user=None):
	user = user or frappe.session.user
	return "" if user == "Administrator" else scope_permission_query(VERSION, user) if _query.get() else "1=0"


def plan_query(user=None):
	user = user or frappe.session.user
	return "" if user == "Administrator" else scope_permission_query(PLAN, user) if _query.get() else "1=0"


def line_query(user=None):
	return "" if (user or frappe.session.user) == "Administrator" else "1=0"


def native_source(doctype: str, name: str | None, fields: set[str], *, user=None, export=False, cache=None):
	key = (doctype, name, tuple(sorted(fields)), user or frappe.session.user, export)
	if cache is not None and key in cache:
		return cache[key]
	result = None
	if name and frappe.db.exists(doctype, name):
		doc = frappe.get_doc(doctype, name)
		if native_read(doc, fields, user=user, export=export):
			result = doc
	if cache is not None:
		cache[key] = result
	return result


def version_sources(doc, *, user=None, export=False, cache=None) -> bool:
	contracts = (
		("Company", doc.company, {"default_currency"}),
		("HRP Hospital", doc.hospital, {"company"}),
		(
			"HRP Organization Version",
			doc.organization_version,
			{"company", "hospital", "status", "effective_from"},
		),
		(
			"Fiscal Year",
			doc.fiscal_year,
			{"year_start_date", "year_end_date", "disabled", "companies.company"},
		),
		("Currency", doc.currency, {"name"}),
	)
	return all(
		native_source(dt, name, fields, user=user, export=export, cache=cache) is not None
		for dt, name, fields in contracts
	)


def version_permission(doc, user=None, ptype=None, debug=False):
	del debug
	user = user or frappe.session.user
	if user == "Guest" or ptype not in {None, "read", "select", "report", "export", "print"}:
		return False
	export = ptype in {"export", "print"}
	if not READ_ROLES.intersection(frappe.get_roles(user)) or not scope_allowed(
		doc, user=user, action="export" if export else "read"
	):
		return False
	fields = set(doc.as_public_dict()) - {"name", "docstatus"}
	return can_read_named_fields(doc, fields, user=user) and version_sources(doc, user=user, export=export)


def plan_permission(doc, user=None, ptype=None, debug=False):
	del debug
	user = user or frappe.session.user
	if user == "Guest" or ptype not in {None, "read", "select", "report", "export", "print"}:
		return False
	export = ptype in {"export", "print"}
	if not READ_ROLES.intersection(frappe.get_roles(user)) or not scope_allowed(
		doc, user=user, action="export" if export else "read"
	):
		return False
	fields = {field.fieldname for field in doc.meta.fields if int(field.permlevel or 0) == 0}
	fields |= {"lines." + field.fieldname for field in frappe.get_meta("HRP Budget Plan Line").fields}
	if not can_read_named_fields(doc, fields, user=user):
		return False
	cache: dict[Any, Any] = {}
	version = native_source(
		VERSION,
		doc.budget_version,
		{
			"company",
			"hospital",
			"currency",
			"currency_precision",
			"fiscal_year",
			"period_start",
			"period_end",
			"organization_version",
		},
		user=user,
		export=export,
		cache=cache,
	)
	if (
		version is None
		or version.company != doc.company
		or version.hospital != doc.hospital
		or not version_sources(version, user=user, export=export, cache=cache)
	):
		return False
	for dt, name, named in (
		(
			"HRP Organization Unit",
			doc.organization_unit,
			{"company", "hospital", "organization_version", "enabled", "valid_from", "valid_to"},
		),
		("Cost Center", doc.cost_center, {"company", "is_group", "disabled"}),
	):
		if native_source(dt, name, named, user=user, export=export, cache=cache) is None:
			return False
	for row in doc.lines or []:
		value = native_source(
			VALUE,
			row.budget_subject,
			{"dimension", "company", "hospital", "enabled", "valid_from", "valid_to"},
			user=user,
			export=export,
			cache=cache,
		)
		if value is None:
			return False
		for dt, name, named in (
			(
				DIMENSION,
				value.dimension,
				{"company", "hospital", "purpose", "enabled", "valid_from", "valid_to"},
			),
			(FUND, row.funding_source, {"company", "hospital", "enabled", "valid_from", "valid_to"}),
			("Account", row.account, {"company", "is_group", "report_type", "disabled"}),
		):
			if native_source(dt, name, named, user=user, export=export, cache=cache) is None:
				return False
		if (
			row.project
			and native_source(
				"Project", row.project, {"company", "status"}, user=user, export=export, cache=cache
			)
			is None
		):
			return False
	return True


def line_permission(doc, user=None, ptype=None, debug=False):
	del debug
	if not doc.parent or doc.parenttype != PLAN or not frappe.db.exists(PLAN, doc.parent):
		return False
	parent = frappe.get_doc(PLAN, doc.parent)
	return frappe.has_permission(PLAN, "read", doc=parent, user=user) and plan_permission(
		parent, user=user, ptype=ptype
	)


def get_version(name: str, *, lock=False, export=False):
	if not frappe.db.exists(VERSION, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	doc = frappe.get_doc(VERSION, name, for_update=lock)
	if export and not frappe.has_permission(VERSION, "export", doc=doc):
		raise_ione_error("PERMISSION_DENIED")
	if not frappe.has_permission(VERSION, "read", doc=doc) or not version_permission(
		doc, ptype="export" if export else "read"
	):
		raise_ione_error("PERMISSION_DENIED")
	return doc


def get_plan(name: str, *, lock=False, export=False):
	if not frappe.db.exists(PLAN, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	doc = frappe.get_doc(PLAN, name, for_update=lock)
	if export and not frappe.has_permission(PLAN, "export", doc=doc):
		raise_ione_error("PERMISSION_DENIED")
	if not frappe.has_permission(PLAN, "read", doc=doc) or not plan_permission(
		doc, ptype="export" if export else "read"
	):
		raise_ione_error("PERMISSION_DENIED")
	return doc


def require_planning_scope(doc, doctype: str, action: str):
	require_scope(
		doctype,
		company=doc.company,
		hospital=doc.hospital,
		organization_unit=doc.get("organization_unit"),
		action=action,
	)
