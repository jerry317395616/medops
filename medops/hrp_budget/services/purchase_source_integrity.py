"""Native PO denial hooks preserving existing Decision bindings, never write authority.

Frappe may already hold native locks before DocEvents. The ordering check below
covers this hook's own queries; it does not claim to order every native writer.
"""

from __future__ import annotations

from contextlib import contextmanager

import frappe

from medops.common.organization import normalize_reference
from medops.common.purchase_source_edit import HEADER_FIELDS, ITEM_FIELDS, validate_bound_purchase_order_edit
from medops.hrp_budget.services.control_guards import (
	invalidate_budget_guards,
	require_purchase_parent_lock_order,
)
from medops.services.errors import raise_ione_error

AUTHORIZATION = "HRP Budget Commitment Authorization"
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


def _parent(name, *, allow_missing=False):
	if normalize_reference(name, label="purchase_order") != name:
		raise ValueError("canonical purchase order required")
	require_purchase_parent_lock_order(name)
	rows = frappe.db.sql(
		"SELECT name, company, supplier, currency, CAST(conversion_rate AS CHAR) AS conversion_rate, "
		"transaction_date, docstatus, status FROM `tabPurchase Order` WHERE name=%s FOR UPDATE",
		(name,),
		as_dict=True,
	)
	if not rows and allow_missing:
		return None
	if len(rows) != 1 or rows[0].name != name:
		raise_ione_error("CONFLICT")
	return rows[0]


def _bindings(parent):
	if not frappe.db.table_exists(AUTHORIZATION):
		if frappe.flags.in_install or frappe.flags.in_migrate:
			return ()
		raise_ione_error("CONFIGURATION_INVALID")
	# Current read after the parent lock: old RR snapshots cannot prove absence.
	# Confirmation-only records do not freeze sources; stale previews fail later.
	rows = frappe.db.sql(
		"SELECT purchase_order_line FROM `tabHRP Budget Commitment Authorization` "
		"WHERE purchase_order=%s AND event_kind='Decision' ORDER BY name LIMIT %s FOR UPDATE",
		(parent, MAX_PARENT_BINDINGS + 1),
		as_dict=True,
	)
	if len(rows) > MAX_PARENT_BINDINGS:
		raise_ione_error("CONFIGURATION_INVALID")
	lines = tuple(sorted({row.purchase_order_line for row in rows}))
	for line in lines:
		if normalize_reference(line, label="purchase_order_line") != line:
			raise ValueError("complete canonical purchase binding required")
	return lines


def _items(parent):
	return frappe.db.sql(
		"SELECT name, parent, parenttype, parentfield, docstatus, material_request, material_request_item, "
		"item_code, uom, cost_center, expense_account, project, is_free_item, "
		"CAST(qty AS CHAR) AS qty, CAST(rate AS CHAR) AS rate, CAST(amount AS CHAR) AS amount, "
		"CAST(base_amount AS CHAR) AS base_amount FROM `tabPurchase Order Item` "
		"WHERE parent=%s AND parenttype='Purchase Order' AND parentfield='items' ORDER BY name FOR UPDATE",
		(parent,),
		as_dict=True,
	)


def protect_purchase_order_edit(doc, method=None, *args, **kwargs):
	del method, args, kwargs
	if doc.is_new() and not doc.name:
		return
	with _boundary():
		before = _parent(doc.name, allow_missing=bool(doc.is_new()))
		if before is None:
			return
		lines = _bindings(before.name)
		if not lines:
			return
		before["items"] = _items(before.name)
		after = {field: doc.get(field) for field in (*HEADER_FIELDS, "docstatus", "status")}
		after["items"] = [{field: row.get(field) for field in ITEM_FIELDS} for row in doc.get("items", ())]
		validate_bound_purchase_order_edit(before, after, lines)


def _child(doc):
	if doc.is_new() and not doc.name:
		return None
	name = normalize_reference(doc.name, label="purchase_order_line")
	if name != doc.name:
		raise ValueError("canonical purchase order line required")
	rows = frappe.db.sql(
		"SELECT name, parent, parenttype, parentfield FROM `tabPurchase Order Item` WHERE name=%s",
		(name,),
		as_dict=True,
	)
	if not rows and doc.is_new():
		return None
	if len(rows) != 1 or (rows[0].name, rows[0].parenttype, rows[0].parentfield) != (
		name,
		"Purchase Order",
		"items",
	):
		raise_ione_error("CONFLICT")
	parent = _parent(rows[0].parent)
	if parent is None:
		raise_ione_error("CONFLICT")
	lines = _bindings(parent.name)
	current = frappe.db.sql(
		"SELECT parent, parenttype, parentfield FROM `tabPurchase Order Item` WHERE name=%s FOR UPDATE",
		(name,),
		as_dict=True,
	)
	if len(current) != 1 or (current[0].parent, current[0].parenttype, current[0].parentfield) != (
		parent.name,
		"Purchase Order",
		"items",
	):
		raise_ione_error("CONFLICT")
	if name not in lines:
		return None
	parent["items"] = _items(parent.name)
	if len([row for row in parent["items"] if row.name == name]) != 1:
		raise_ione_error("CONFLICT")
	return parent, lines


def protect_purchase_order_item_edit(doc, method=None, *args, **kwargs):
	del method, args, kwargs
	with _boundary():
		bound = _child(doc)
		if bound is None:
			return
		before, lines = bound
		after = dict(before)
		after["items"] = [
			{field: doc.get(field) for field in ITEM_FIELDS} if row.name == doc.name else row
			for row in before["items"]
		]
		validate_bound_purchase_order_edit(before, after, lines)


def protect_purchase_order_lifecycle(doc, method=None, *args, **kwargs):
	del args, kwargs
	with _boundary():
		if method == "before_rename":
			doc.check_permission("write")
		_parent(doc.name)
		if _bindings(doc.name):
			raise_ione_error("OPERATION_NOT_ALLOWED")


def protect_purchase_order_item_lifecycle(doc, method=None, *args, **kwargs):
	del args, kwargs
	with _boundary():
		if method == "before_rename":
			doc.check_permission("write")
		if _child(doc) is not None:
			raise_ione_error("OPERATION_NOT_ALLOWED")
