from __future__ import annotations

from collections.abc import Collection
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import frappe
from frappe.model.document import Document

from medops.common.master_data import MASTER_DATA_TARGET_POLICIES
from medops.common.master_data_security import evaluation_fields
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.data_access import public_fields_readable as public_fields_readable
from medops.services.data_access import require_scope as require_scope
from medops.services.data_access import scope_allowed as scope_allowed
from medops.services.errors import raise_ione_error

SCOPED_MASTER_TYPES = frozenset(
	{
		"HRP Master Data Request",
		"HRP External Code Mapping",
		"HRP Data Quality Rule",
		"HRP Data Quality Issue",
	}
)
GLOBAL_DOMAIN_ADMIN_ROLES = frozenset({"System Manager", "HRP System Manager"})
_scoped_list: ContextVar[bool] = ContextVar("medops_scoped_master_data_list", default=False)


def scoped_list_active() -> bool:
	return _scoped_list.get()


@contextmanager
def scoped_list_query():
	token = _scoped_list.set(True)
	try:
		yield
	finally:
		_scoped_list.reset(token)


def target_readable(
	doctype: str,
	name: str,
	*,
	fields: Collection[str] = (),
	company: str | None = None,
	user: str | None = None,
	action: str = "read",
	lock: bool = False,
) -> bool:
	user = user or frappe.session.user
	if not frappe.db.exists("DocType", doctype) or not frappe.db.exists(doctype, name):
		return False
	target = frappe.get_doc(doctype, name, for_update=lock)
	policy = MASTER_DATA_TARGET_POLICIES.get(doctype)
	if policy and policy.company_field and company and target.get(policy.company_field) != company:
		return False
	if not frappe.has_permission(doctype, ptype="read", doc=target, user=user):
		return False
	if action == "export" and not frappe.has_permission(doctype, ptype="export", doc=target, user=user):
		return False
	# Standard shared Item/Supplier records do not carry hospital dimensions. Their governed
	# parent supplies the hospital boundary; native permissions still govern the actual target.
	checked = set(fields)
	if policy and policy.company_field:
		checked.add(policy.company_field)
	return can_read_named_fields(target, checked, user=user)


def require_target_read(
	doctype: str, name: str, *, fields: Collection[str] = (), company: str | None = None, lock: bool = False
) -> None:
	if not target_readable(doctype, name, fields=fields, lock=lock):
		raise_ione_error("PERMISSION_DENIED")
	policy = MASTER_DATA_TARGET_POLICIES.get(doctype)
	if (
		policy
		and policy.company_field
		and company
		and frappe.db.get_value(doctype, name, policy.company_field, for_update=lock) != company
	):
		raise_ione_error("CONFLICT")


def source_fields_readable(doc: Document, *, user: str | None = None, action: str = "read") -> bool:
	if (user or frappe.session.user) == "Administrator":
		return True
	if doc.doctype == "HRP Master Data Request":
		fields = {row.field_name for row in doc.changes}
		if not doc.target_name:
			probe = frappe.get_doc({"doctype": doc.target_doctype, "owner": doc.requested_by})
			return can_read_named_fields(probe, fields, user=user)
		return target_readable(
			doc.target_doctype, doc.target_name, fields=fields, company=doc.company, user=user, action=action
		)
	if doc.doctype == "HRP External Code Mapping":
		return target_readable(
			doc.target_doctype,
			doc.internal_name,
			fields={"disabled"},
			company=doc.company,
			user=user,
			action=action,
		)
	if doc.doctype == "HRP Data Quality Issue":
		try:
			fields = evaluation_fields(doc.get("evaluation_read_fields"), doc.target_doctype)
		except ValueError:
			return False
		return target_readable(
			doc.target_doctype,
			doc.target_name,
			fields=set(fields),
			company=doc.company,
			user=user,
			action=action,
		)
	return True


def master_data_visible(doc: Document, *, user: str | None = None, action: str = "read") -> bool:
	return (
		scope_allowed(doc, user=user, action=action)
		and public_fields_readable(doc, user=user)
		and source_fields_readable(doc, user=user, action=action)
	)


def require_document_read(doc: Document) -> None:
	if not frappe.has_permission(doc.doctype, ptype="read", doc=doc) or not master_data_visible(doc):
		raise_ione_error("PERMISSION_DENIED")


def get_scoped_documents(
	doctype: str,
	*,
	filters: dict[str, Any] | None = None,
	or_filters: list[Any] | None = None,
	maximum: int = 5000,
) -> list[Document]:
	if doctype not in SCOPED_MASTER_TYPES or not 1 <= maximum <= 5000:
		raise_ione_error("INVALID_REQUEST")
	if not frappe.has_permission(doctype, ptype="read"):
		raise_ione_error("PERMISSION_DENIED")
	with scoped_list_query():
		rows = frappe.get_list(
			doctype,
			filters=filters or {},
			or_filters=or_filters,
			fields=["name"],
			order_by="modified desc, name desc",
			limit_page_length=maximum + 1,
		)
	if len(rows) > maximum:
		raise_ione_error("INVALID_REQUEST")
	result = []
	for row in rows:
		doc = frappe.get_doc(doctype, row.name)
		if frappe.has_permission(doctype, ptype="read", doc=doc) and master_data_visible(doc):
			result.append(doc)
	return result
