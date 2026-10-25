from __future__ import annotations

import frappe
from frappe.model.document import Document

from medops.hrp_foundation.services.numbering import (
	NUMBERING_ADMIN_ROLES,
	NUMBERING_READ_ROLES,
)
from medops.services.data_access import public_fields_readable, scope_allowed, scope_permission_query


def _roles(user: str) -> set[str]:
	return set(frappe.get_roles(user))


def numbering_scheme_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Guest":
		return "1=0"
	return (
		scope_permission_query("HRP Numbering Scheme", user)
		if NUMBERING_READ_ROLES.intersection(_roles(user))
		else "1=0"
	)


def can_read_numbering_scheme(
	doc: Document,
	user: str | None = None,
	ptype: str | None = None,
	debug: bool = False,
) -> bool:
	del debug
	user = user or frappe.session.user
	return (
		user != "Guest"
		and ptype in {None, "read"}
		and bool(NUMBERING_READ_ROLES.intersection(_roles(user)))
		and scope_allowed(doc, user=user)
		and public_fields_readable(doc, user=user)
	)


def number_reservation_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Guest":
		return "1=0"
	roles = _roles(user)
	if NUMBERING_ADMIN_ROLES.intersection(roles) or "HRP Auditor" in roles:
		return scope_permission_query("HRP Number Reservation", user)
	if NUMBERING_READ_ROLES.intersection(roles):
		condition = scope_permission_query("HRP Number Reservation", user)
		owner = f"`tabHRP Number Reservation`.`allocated_by` = {frappe.db.escape(user)}"
		return f"({condition}) AND ({owner})" if condition else owner
	return "1=0"


def can_read_number_reservation(
	doc: Document,
	user: str | None = None,
	ptype: str | None = None,
	debug: bool = False,
) -> bool:
	del debug
	user = user or frappe.session.user
	if user == "Guest" or ptype not in {None, "read"}:
		return False
	roles = _roles(user)
	if NUMBERING_ADMIN_ROLES.intersection(roles) or "HRP Auditor" in roles:
		return scope_allowed(doc, user=user) and public_fields_readable(doc, user=user)
	return (
		bool(NUMBERING_READ_ROLES.intersection(roles))
		and doc.allocated_by == user
		and scope_allowed(doc, user=user)
		and public_fields_readable(doc, user=user)
	)


__all__ = [
	"can_read_number_reservation",
	"can_read_numbering_scheme",
	"number_reservation_query",
	"numbering_scheme_query",
]
