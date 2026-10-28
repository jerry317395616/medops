"""Native MR save/db_set/lifecycle hooks preserving immutable source bindings.

Only denial and exact comparison: no custom source writes or financial grants.
The native outer transaction owns rollback and any whole-transaction retry.
"""

from __future__ import annotations

from contextlib import contextmanager

import frappe

from medops.common.material_source_edit import (
	HEADER_FIELDS,
	ITEM_FIELDS,
	validate_bound_material_request_edit,
)
from medops.common.organization import normalize_reference
from medops.hrp_budget.services.control_guards import (
	invalidate_budget_guards,
	require_material_parent_lock_order,
)
from medops.services.errors import raise_ione_error

ROOT = "HRP Budget Reservation"
MAX_PARENT_BINDINGS = 10000


@contextmanager
def _boundary():
	try:
		yield
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("OPERATION_NOT_ALLOWED", cause=error)
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise


def _parent(name):
	name = normalize_reference(name, label="material_request")
	require_material_parent_lock_order(name)
	rows = frappe.db.sql(
		"SELECT name, company, material_request_type, transaction_date, docstatus, status "
		"FROM `tabMaterial Request` WHERE name=%s FOR UPDATE",
		(name,),
		as_dict=True,
	)
	if len(rows) != 1:
		raise_ione_error("CONFLICT")
	return rows[0]


def _bindings(parent):
	if not frappe.db.table_exists(ROOT):
		if frappe.flags.in_install or frappe.flags.in_migrate:
			return ()
		raise_ione_error("CONFIGURATION_INVALID")
	# A current read after the native parent lock also works for ordinary native
	# REPEATABLE READ requests that observed an earlier, unreserved snapshot.
	rows = frappe.db.sql(
		"SELECT source_line FROM `tabHRP Budget Reservation` "
		"WHERE source_doctype='Material Request' AND source_name=%s "
		"ORDER BY name LIMIT %s FOR UPDATE",
		(parent, MAX_PARENT_BINDINGS + 1),
		as_dict=True,
	)
	if len(rows) > MAX_PARENT_BINDINGS:
		raise_ione_error("CONFIGURATION_INVALID")
	lines = tuple(sorted({row.source_line for row in rows}))
	for line in lines:
		if normalize_reference(line, label="source_line") != line:
			raise ValueError("complete canonical binding required")
	return lines


def _items(parent):
	return frappe.db.sql(
		"SELECT name, parent, parenttype, parentfield, item_code, uom, cost_center, expense_account, project, "
		"CAST(qty AS CHAR) AS qty, CAST(rate AS CHAR) AS rate, CAST(amount AS CHAR) AS amount "
		"FROM `tabMaterial Request Item` "
		"WHERE parent=%s AND parenttype='Material Request' AND parentfield='items' ORDER BY name FOR UPDATE",
		(parent,),
		as_dict=True,
	)


def protect_material_request_edit(doc, method=None, *args, **kwargs):
	del method, args, kwargs
	if doc.is_new():
		return
	with _boundary():
		before = _parent(doc.name)
		lines = _bindings(doc.name)
		if not lines:
			return
		before["items"] = _items(doc.name)
		after = {field: doc.get(field) for field in (*HEADER_FIELDS, "docstatus", "status")}
		after["items"] = [{field: row.get(field) for field in ITEM_FIELDS} for row in doc.get("items", ())]
		validate_bound_material_request_edit(before, after, lines)


def _child(doc):
	name = normalize_reference(doc.name, label="material_request_line")
	rows = frappe.db.sql(
		"SELECT parent FROM `tabMaterial Request Item` WHERE name=%s "
		"AND parenttype='Material Request' AND parentfield='items'",
		(name,),
		as_dict=True,
	)
	if len(rows) != 1:
		raise_ione_error("CONFLICT")
	parent = _parent(rows[0].parent)
	current = frappe.db.sql(
		"SELECT parent, parenttype, parentfield FROM `tabMaterial Request Item` WHERE name=%s FOR UPDATE",
		(name,),
		as_dict=True,
	)
	if len(current) != 1 or (
		current[0].parent,
		current[0].parenttype,
		current[0].parentfield,
	) != (parent.name, "Material Request", "items"):
		raise_ione_error("CONFLICT")
	lines = _bindings(parent.name)
	if name not in lines:
		return None
	parent["items"] = _items(parent.name)
	if len([row for row in parent["items"] if row.name == name]) != 1:
		raise_ione_error("CONFLICT")
	return parent, lines


def protect_material_request_item_edit(doc, method=None, *args, **kwargs):
	del method, args, kwargs
	if doc.is_new():
		return
	with _boundary():
		bound = _child(doc)
		if bound is None:
			return
		before, lines = bound
		if (doc.parent, doc.parenttype, doc.parentfield) != (before.name, "Material Request", "items"):
			raise ValueError("registered native child cannot move")
		after = dict(before)
		after["items"] = [
			{field: doc.get(field) for field in ITEM_FIELDS} if row.name == doc.name else row
			for row in before["items"]
		]
		validate_bound_material_request_edit(before, after, lines)


def protect_material_request_lifecycle(doc, method=None, *args, **kwargs):
	del args, kwargs
	if method == "before_rename":
		# Native rename calls this before its own permission validation.
		doc.check_permission("write")
	with _boundary():
		_parent(doc.name)
		if _bindings(doc.name):
			raise_ione_error("OPERATION_NOT_ALLOWED")


def protect_material_request_item_lifecycle(doc, method=None, *args, **kwargs):
	del args, kwargs
	if method == "before_rename":
		doc.check_permission("write")
	with _boundary():
		if _child(doc) is not None:
			raise_ione_error("OPERATION_NOT_ALLOWED")
