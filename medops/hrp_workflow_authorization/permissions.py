from __future__ import annotations

from typing import Protocol

import frappe

from medops.common.access_scope import (
	AccessAction,
	ScopeFilterCondition,
	ScopeFilterGroup,
	filter_group_for,
	normalize_action,
)
from medops.hrp_workflow_authorization.services.access_scope import (
	ACCESS_SCOPE_ADMIN_ROLES,
	dimension_fields_for,
	grants_for,
)


class _ScopedDocument(Protocol):
	doctype: str

	def get(self, key: str) -> object: ...


def _is_unrestricted(user: str) -> bool:
	return user == "Administrator" or bool(ACCESS_SCOPE_ADMIN_ROLES.intersection(frappe.get_roles(user)))


def _quoted_table(doctype: str) -> str:
	return f"`tab{doctype.replace('`', '``')}`"


def _condition_sql(doctype: str, group: ScopeFilterGroup) -> str:
	table = _quoted_table(doctype)
	parts: list[str] = []
	for condition in group["conditions"]:
		field = str(condition["field"]).replace("`", "``")
		operator = condition["operator"]
		value = condition["value"]
		if operator == "=":
			parts.append(f"{table}.`{field}` = {frappe.db.escape(value)}")
		elif operator == "in" and isinstance(value, list) and value:
			values = ", ".join(frappe.db.escape(item) for item in value)
			parts.append(f"{table}.`{field}` IN ({values})")
		else:
			return "1=0"
	return "(" + " AND ".join(parts) + ")"


def scope_permission_query(
	doctype: str,
	user: str | None = None,
	*,
	action: AccessAction = "read",
) -> str:
	user = user or frappe.session.user
	if user == "Guest":
		return "1=0"
	if _is_unrestricted(user):
		return ""
	dimensions = dimension_fields_for(doctype)
	groups = [
		group
		for grant in grants_for(user, doctype, action)
		if (group := filter_group_for(grant, dimension_fields=dimensions)) is not None
	]
	if not groups:
		return "1=0"
	return "(" + " OR ".join(_condition_sql(doctype, group) for group in groups) + ")"


def has_scoped_permission(
	doc: _ScopedDocument,
	user: str | None = None,
	ptype: str | None = None,
	debug: bool = False,
) -> bool:
	del debug
	user = user or frappe.session.user
	if user == "Guest":
		return False
	if _is_unrestricted(user):
		return True
	try:
		action = normalize_action(ptype or "read")
	except ValueError:
		return False
	dimensions = dimension_fields_for(doc.doctype)
	for grant in grants_for(user, doc.doctype, action):
		group = filter_group_for(grant, dimension_fields=dimensions)
		if group is None:
			continue
		conditions = group.get("conditions", [])
		if isinstance(conditions, list) and all(
			isinstance(condition, dict) and _document_matches(doc, condition) for condition in conditions
		):
			return True
	return False


def _document_matches(doc: _ScopedDocument, condition: ScopeFilterCondition) -> bool:
	actual = doc.get(str(condition["field"]))
	if condition["operator"] == "=":
		return actual == condition["value"]
	if condition["operator"] == "in" and isinstance(condition["value"], list):
		return actual in condition["value"]
	return False


def hospital_query(user: str | None = None) -> str:
	return scope_permission_query("HRP Hospital", user)


def organization_version_query(user: str | None = None) -> str:
	return scope_permission_query("HRP Organization Version", user)


def organization_unit_query(user: str | None = None) -> str:
	return scope_permission_query("HRP Organization Unit", user)


def organization_mapping_query(user: str | None = None) -> str:
	return scope_permission_query("HRP Organization Mapping", user)


def approval_matrix_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user != "Guest" and {"System Manager", "HRP System Manager", "HRP Auditor"}.intersection(
		frappe.get_roles(user)
	):
		return ""
	return "1=0"


def can_read_approval_matrix(
	doc: _ScopedDocument,
	user: str | None = None,
	ptype: str | None = None,
	debug: bool = False,
) -> bool:
	del doc, debug
	user = user or frappe.session.user
	allowed = {"System Manager", "HRP System Manager"}
	if ptype in (None, "read", "report", "export", "print", "email"):
		allowed.add("HRP Auditor")
	return user != "Guest" and bool(allowed.intersection(frappe.get_roles(user)))


def segregation_rule_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user != "Guest" and {"System Manager", "HRP System Manager", "HRP Auditor"}.intersection(
		frappe.get_roles(user)
	):
		return ""
	return "1=0"


def can_read_segregation_rule(
	doc: _ScopedDocument,
	user: str | None = None,
	ptype: str | None = None,
	debug: bool = False,
) -> bool:
	del doc, debug
	user = user or frappe.session.user
	allowed = {"System Manager", "HRP System Manager"}
	if ptype in (None, "read", "report", "export", "print", "email"):
		allowed.add("HRP Auditor")
	return user != "Guest" and bool(allowed.intersection(frappe.get_roles(user)))


def delegation_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Guest":
		return "1=0"
	if {"System Manager", "HRP System Manager", "HRP Auditor"}.intersection(frappe.get_roles(user)):
		return ""
	table = _quoted_table("HRP Delegation")
	escaped = frappe.db.escape(user)
	return f"({table}.`from_user` = {escaped} OR {table}.`to_user` = {escaped})"


def can_read_delegation(
	doc: _ScopedDocument,
	user: str | None = None,
	ptype: str | None = None,
	debug: bool = False,
) -> bool:
	del debug
	user = user or frappe.session.user
	if user == "Guest":
		return False
	if ptype not in (None, "read", "report", "export", "print", "email"):
		return False
	if {"System Manager", "HRP System Manager", "HRP Auditor"}.intersection(frappe.get_roles(user)):
		return True
	return user in {doc.get("from_user"), doc.get("to_user")}


__all__ = [
	"approval_matrix_query",
	"can_read_approval_matrix",
	"can_read_delegation",
	"can_read_segregation_rule",
	"delegation_query",
	"has_scoped_permission",
	"hospital_query",
	"organization_mapping_query",
	"organization_unit_query",
	"organization_version_query",
	"scope_permission_query",
	"segregation_rule_query",
]
