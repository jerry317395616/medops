from __future__ import annotations

from typing import Protocol

import frappe

from medops.hrp_master_data.services.data_quality import DATA_QUALITY_READ_ROLES
from medops.hrp_master_data.services.external_code_mapping import (
	EXTERNAL_CODE_MAPPING_READ_ROLES,
)
from medops.hrp_master_data.services.master_data import MASTER_DATA_ADMIN_ROLES


class _MasterDataRequest(Protocol):
	requested_by: str


def _user_roles(user: str) -> set[str]:
	return set(frappe.get_roles(user))


def master_data_request_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Guest":
		return "1=0"
	roles = _user_roles(user)
	if MASTER_DATA_ADMIN_ROLES.intersection(roles):
		return ""
	if "HRP User" not in roles:
		return "1=0"
	return f"`tabHRP Master Data Request`.`requested_by` = {frappe.db.escape(user)}"


def can_read_master_data_request(
	doc: _MasterDataRequest,
	user: str | None = None,
	ptype: str | None = None,
	debug: bool = False,
) -> bool:
	del debug
	user = user or frappe.session.user
	if user == "Guest" or ptype not in {None, "read"}:
		return False
	roles = _user_roles(user)
	if MASTER_DATA_ADMIN_ROLES.intersection(roles):
		return True
	return "HRP User" in roles and doc.requested_by == user


def external_code_mapping_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Guest":
		return "1=0"
	return "" if EXTERNAL_CODE_MAPPING_READ_ROLES.intersection(_user_roles(user)) else "1=0"


def can_read_external_code_mapping(
	doc: object,
	user: str | None = None,
	ptype: str | None = None,
	debug: bool = False,
) -> bool:
	del doc, debug
	user = user or frappe.session.user
	if user == "Guest" or ptype not in {None, "read"}:
		return False
	return bool(EXTERNAL_CODE_MAPPING_READ_ROLES.intersection(_user_roles(user)))


def data_quality_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Guest":
		return "1=0"
	return "" if DATA_QUALITY_READ_ROLES.intersection(_user_roles(user)) else "1=0"


def can_read_data_quality(
	doc: object,
	user: str | None = None,
	ptype: str | None = None,
	debug: bool = False,
) -> bool:
	del doc, debug
	user = user or frappe.session.user
	if user == "Guest" or ptype not in {None, "read"}:
		return False
	return bool(DATA_QUALITY_READ_ROLES.intersection(_user_roles(user)))


__all__ = [
	"can_read_data_quality",
	"can_read_external_code_mapping",
	"can_read_master_data_request",
	"data_quality_query",
	"external_code_mapping_query",
	"master_data_request_query",
]
