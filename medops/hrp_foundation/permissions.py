from __future__ import annotations

from typing import Protocol

import frappe

from medops.hrp_foundation.services.numbering import (
	NUMBERING_ADMIN_ROLES,
	NUMBERING_READ_ROLES,
)


class _NumberReservation(Protocol):
	allocated_by: str


def _roles(user: str) -> set[str]:
	return set(frappe.get_roles(user))


def numbering_scheme_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Guest":
		return "1=0"
	return "" if NUMBERING_READ_ROLES.intersection(_roles(user)) else "1=0"


def can_read_numbering_scheme(
	doc: object,
	user: str | None = None,
	ptype: str | None = None,
	debug: bool = False,
) -> bool:
	del doc, debug
	user = user or frappe.session.user
	return (
		user != "Guest" and ptype in {None, "read"} and bool(NUMBERING_READ_ROLES.intersection(_roles(user)))
	)


def number_reservation_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Guest":
		return "1=0"
	roles = _roles(user)
	if NUMBERING_ADMIN_ROLES.intersection(roles) or "HRP Auditor" in roles:
		return ""
	if NUMBERING_READ_ROLES.intersection(roles):
		return f"`tabHRP Number Reservation`.`allocated_by` = {frappe.db.escape(user)}"
	return "1=0"


def can_read_number_reservation(
	doc: _NumberReservation,
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
		return True
	return bool(NUMBERING_READ_ROLES.intersection(roles)) and doc.allocated_by == user


__all__ = [
	"can_read_number_reservation",
	"can_read_numbering_scheme",
	"number_reservation_query",
	"numbering_scheme_query",
]
