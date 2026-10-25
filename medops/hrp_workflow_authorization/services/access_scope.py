from __future__ import annotations

from datetime import date

import frappe

from medops.common.access_scope import (
	AccessAction,
	ScopeDecision,
	ScopeGrant,
	build_scope_decision,
	normalize_action,
	normalize_doctype,
)
from medops.services.audit_context import emit_audit_event
from medops.services.errors import raise_ione_error, require_authenticated_user

ACCESS_SCOPE_ADMIN_ROLES = frozenset({"System Manager", "HRP System Manager"})
SPECIAL_DIMENSION_FIELDS: dict[str, dict[str, str]] = {
	"Company": {"company": "name"},
	"HRP Hospital": {"company": "company", "hospital": "name"},
	"HRP Organization Unit": {
		"company": "company",
		"hospital": "hospital",
		"organization_unit": "name",
	},
	"HRP Organization Version": {"company": "company", "hospital": "hospital"},
}


def dimension_fields_for(doctype: str) -> dict[str, str]:
	meta = frappe.get_meta(doctype)
	fields = {field.fieldname for field in meta.fields}
	fields.add("name")
	if doctype in SPECIAL_DIMENSION_FIELDS:
		mapping = SPECIAL_DIMENSION_FIELDS[doctype]
	else:
		mapping = {
			dimension: dimension
			for dimension in ("company", "hospital", "organization_unit")
			if dimension in fields
		}
	if any(fieldname not in fields for fieldname in mapping.values()):
		raise_ione_error("CONFIGURATION_INVALID")
	return mapping


def _expand_organization_units(
	organization_unit: str,
	*,
	include_descendants: bool,
) -> tuple[str, ...]:
	if not include_descendants:
		return (organization_unit,)
	root = frappe.db.get_value(
		"HRP Organization Unit",
		organization_unit,
		["organization_version", "lft", "rgt"],
		as_dict=True,
	)
	if not root or root.lft is None or root.rgt is None:
		raise_ione_error("CONFIGURATION_INVALID")
	units = frappe.get_all(
		"HRP Organization Unit",
		filters={
			"organization_version": root.organization_version,
			"lft": (">=", root.lft),
			"rgt": ("<=", root.rgt),
			"enabled": 1,
		},
		order_by="lft asc",
		pluck="name",
	)
	if organization_unit not in units:
		raise_ione_error("CONFIGURATION_INVALID")
	return tuple(units)


def grants_for(user: str, doctype: str, action: AccessAction) -> tuple[ScopeGrant, ...]:
	action_field = f"allow_{action}"
	today = date.today().isoformat()
	rows = frappe.db.sql(
		f"""
		SELECT
			scope.code,
			scope.company,
			scope.hospital,
			scope.organization_unit,
			scope.include_descendants
		FROM `tabHRP Access Scope` AS scope
		INNER JOIN `tabHRP Access Scope Member` AS member
			ON member.parent = scope.name
			AND member.parenttype = 'HRP Access Scope'
			AND member.parentfield = 'members'
		WHERE
			scope.enabled = 1
			AND member.enabled = 1
			AND member.user = %s
			AND (member.target_doctype IS NULL OR member.target_doctype = '' OR member.target_doctype = %s)
			AND member.`{action_field}` = 1
			AND (scope.valid_from IS NULL OR scope.valid_from <= %s)
			AND (scope.valid_to IS NULL OR scope.valid_to >= %s)
		ORDER BY scope.code
		""",
		(user, doctype, today, today),
		as_dict=True,
	)
	grants: list[ScopeGrant] = []
	seen: set[tuple[str, str, str | None, str | None]] = set()
	for row in rows:
		key = (row.code, row.company, row.hospital or None, row.organization_unit or None)
		if key in seen:
			continue
		seen.add(key)
		units = (
			_expand_organization_units(
				row.organization_unit,
				include_descendants=bool(row.include_descendants),
			)
			if row.organization_unit
			else ()
		)
		grants.append(
			ScopeGrant(
				code=row.code,
				company=row.company,
				hospital=row.hospital or None,
				organization_unit=row.organization_unit or None,
				include_descendants=bool(row.include_descendants),
				organization_units=units,
			)
		)
	return tuple(grants)


def resolve_access_scope(
	*,
	user: object | None,
	doctype: object,
	action: object,
) -> ScopeDecision:
	require_authenticated_user()
	target_user = frappe.session.user if user in (None, "") else str(user)
	roles = set(frappe.get_roles())
	if target_user != frappe.session.user and not ACCESS_SCOPE_ADMIN_ROLES.intersection(roles):
		raise_ione_error("PERMISSION_DENIED")
	if target_user == "Guest" or not frappe.db.exists("User", target_user):
		raise_ione_error("RESOURCE_NOT_FOUND")
	try:
		target_doctype = normalize_doctype(doctype)
		target_action = normalize_action(action)
	except ValueError as exc:
		raise_ione_error("INVALID_REQUEST", cause=exc)
	if not frappe.db.exists("DocType", target_doctype):
		raise_ione_error("RESOURCE_NOT_FOUND")

	target_roles = set(frappe.get_roles(target_user))
	unrestricted = target_user == "Administrator" or bool(ACCESS_SCOPE_ADMIN_ROLES.intersection(target_roles))
	permission_type = "read" if target_action == "export" else target_action
	base_permission = bool(frappe.has_permission(target_doctype, ptype=permission_type, user=target_user))
	if target_action == "export":
		meta = frappe.get_meta(target_doctype)
		base_permission = base_permission and any(
			permission.role in target_roles and bool(permission.export) for permission in meta.permissions
		)
	grants = () if unrestricted else grants_for(target_user, target_doctype, target_action)
	decision = build_scope_decision(
		user=target_user,
		doctype=target_doctype,
		action=target_action,
		dimension_fields=dimension_fields_for(target_doctype),
		grants=grants,
		base_permission=base_permission,
		unrestricted=unrestricted,
	)
	emit_audit_event(
		"access_scope_resolved",
		logger_name="medops.access_scope",
		target_doctype=target_doctype,
		action=target_action,
		allowed=decision["allowed"],
		unrestricted=decision["unrestricted"],
		scope_count=len(decision["scopes"]),
		decision_digest=decision["decision_digest"],
	)
	return decision


__all__ = [
	"ACCESS_SCOPE_ADMIN_ROLES",
	"SPECIAL_DIMENSION_FIELDS",
	"dimension_fields_for",
	"grants_for",
	"resolve_access_scope",
]
