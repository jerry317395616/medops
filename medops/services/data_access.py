from __future__ import annotations

from typing import Any

import frappe
from frappe.model.document import Document

from medops.hrp_workflow_authorization.permissions import (
	has_scoped_permission,
)
from medops.hrp_workflow_authorization.permissions import (
	scope_permission_query as scope_permission_query,
)
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.errors import raise_ione_error


def scope_allowed(doc: Any, *, action: str = "read", user: str | None = None) -> bool:
	return has_scoped_permission(doc, user=user, ptype="read") and (
		action == "read" or has_scoped_permission(doc, user=user, ptype=action)
	)


def require_scope(
	doctype: str,
	*,
	company: str | None,
	hospital: str | None,
	organization_unit: str | None,
	action: str = "read",
) -> None:
	name = {"Company": company, "HRP Hospital": hospital, "HRP Organization Unit": organization_unit}.get(
		doctype
	)
	probe = frappe._dict(
		doctype=doctype, name=name, company=company, hospital=hospital, organization_unit=organization_unit
	)
	if not scope_allowed(probe, action=action):
		raise_ione_error("PERMISSION_DENIED")


def require_scoped_read(doc: Document) -> None:
	if not frappe.has_permission(doc.doctype, ptype="read", doc=doc) or not scope_allowed(doc):
		raise_ione_error("PERMISSION_DENIED")


def public_fields_readable(doc: Document, *, user: str | None = None) -> bool:
	try:
		public = doc.as_public_dict()
		fields = {key for key in public if doc.meta.has_field(key)}
		if "parameters" in public:
			fields.add("parameters_json")
		for alias, field in {
			"dimensions": "dimensions_json",
			"dimension_keys": "dimension_keys_json",
			"scheme": "scheme_code",
			"date": "business_date",
		}.items():
			if alias in public and doc.meta.has_field(field):
				fields.add(field)
		if "changes" in public:
			child = frappe.get_meta(doc.meta.get_field("changes").options)
			fields.update(
				"changes." + key for row in public["changes"] for key in row if child.has_field(key)
			)
		return can_read_named_fields(doc, fields, user=user)
	except (ValueError, KeyError, TypeError):
		return False
