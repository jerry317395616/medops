from __future__ import annotations

import frappe
from frappe.model.document import Document

from medops.hrp_master_data.services.access import master_data_visible, scoped_list_active
from medops.hrp_master_data.services.data_quality import DATA_QUALITY_READ_ROLES
from medops.hrp_master_data.services.external_code_mapping import (
	EXTERNAL_CODE_MAPPING_READ_ROLES,
)
from medops.hrp_master_data.services.master_data import MASTER_DATA_ADMIN_ROLES
from medops.services.data_access import scope_permission_query


def _user_roles(user: str) -> set[str]:
	return set(frappe.get_roles(user))


def master_data_request_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Guest":
		return "1=0"
	# Generic SQL lists cannot enforce permissions on copied target-field snapshots.
	# Other users use the scoped, per-document facade.
	if user == "Administrator":
		return ""
	if not scoped_list_active():
		return "1=0"
	roles = _user_roles(user)
	if not MASTER_DATA_ADMIN_ROLES.intersection(roles) and "HRP User" not in roles:
		return "1=0"
	parts = [scope_permission_query("HRP Master Data Request", user)]
	if not MASTER_DATA_ADMIN_ROLES.intersection(roles):
		parts.append(f"`tabHRP Master Data Request`.`requested_by` = {frappe.db.escape(user)}")
	return " AND ".join(f"({part})" for part in parts if part) or ""


def can_read_master_data_request(
	doc: Document,
	user: str | None = None,
	ptype: str | None = None,
	debug: bool = False,
) -> bool:
	del debug
	user = user or frappe.session.user
	if user == "Guest" or ptype not in {None, "read", "select", "export", "print"}:
		return False
	roles = _user_roles(user)
	if not MASTER_DATA_ADMIN_ROLES.intersection(roles) and not (
		"HRP User" in roles and doc.requested_by == user
	):
		return False
	return master_data_visible(doc, user=user, action="export" if ptype in {"export", "print"} else "read")


def external_code_mapping_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Guest":
		return "1=0"
	if user == "Administrator":
		return ""
	return (
		scope_permission_query("HRP External Code Mapping", user)
		if scoped_list_active() and EXTERNAL_CODE_MAPPING_READ_ROLES.intersection(_user_roles(user))
		else "1=0"
	)


def can_read_external_code_mapping(
	doc: Document,
	user: str | None = None,
	ptype: str | None = None,
	debug: bool = False,
) -> bool:
	del debug
	user = user or frappe.session.user
	if user == "Guest" or ptype not in {None, "read", "select", "export", "print"}:
		return False
	return bool(EXTERNAL_CODE_MAPPING_READ_ROLES.intersection(_user_roles(user))) and master_data_visible(
		doc, user=user, action="export" if ptype in {"export", "print"} else "read"
	)


def data_quality_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Guest":
		return "1=0"
	if user == "Administrator":
		return ""
	# Rule/issue wrappers below choose the actual reviewed table name.
	return "1=0"


def data_quality_rule_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	return (
		scope_permission_query("HRP Data Quality Rule", user)
		if user != "Guest"
		and scoped_list_active()
		and DATA_QUALITY_READ_ROLES.intersection(_user_roles(user))
		else "1=0"
	)


def data_quality_issue_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	return (
		scope_permission_query("HRP Data Quality Issue", user)
		if user != "Guest"
		and scoped_list_active()
		and DATA_QUALITY_READ_ROLES.intersection(_user_roles(user))
		else "1=0"
	)


def can_read_data_quality(
	doc: Document,
	user: str | None = None,
	ptype: str | None = None,
	debug: bool = False,
) -> bool:
	del debug
	user = user or frappe.session.user
	if user == "Guest" or ptype not in {None, "read", "select", "export", "print"}:
		return False
	return bool(DATA_QUALITY_READ_ROLES.intersection(_user_roles(user))) and master_data_visible(
		doc, user=user, action="export" if ptype in {"export", "print"} else "read"
	)


__all__ = [
	"can_read_data_quality",
	"can_read_external_code_mapping",
	"can_read_master_data_request",
	"data_quality_query",
	"external_code_mapping_query",
	"master_data_request_query",
]
