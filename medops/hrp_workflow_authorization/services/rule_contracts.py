from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

import frappe
from frappe.model.document import Document

from medops.common.budget_planning import DECIMAL_DATA_FIELDS
from medops.common.business_rules import (
	MAX_FIELD_PATHS,
	NUMBER_TYPES,
	TEXT_TYPES,
	BusinessRuleError,
	field_paths,
	normalize_tree,
	validate_tree_contract,
)
from medops.common.domain_service import canonical_json
from medops.hrp_workflow_authorization.services.access_scope import dimension_fields_for
from medops.services.errors import raise_ione_error

SYSTEM_FIELDS = {
	"docstatus": ("Int", ""),
	"owner": ("Link", "User"),
	"creation": ("Datetime", ""),
	"modified": ("Datetime", ""),
}


def _field_contract(doctype: str, fieldname: str, *, allow_table: bool = False) -> dict[str, Any]:
	meta = frappe.get_meta(doctype)
	field = meta.get_field(fieldname)
	if field is None and fieldname in SYSTEM_FIELDS and not meta.istable:
		fieldtype, options = SYSTEM_FIELDS[fieldname]
		level = masked = 0
	else:
		if not field or field.is_virtual:
			raise BusinessRuleError("field is unavailable")
		fieldtype, options = field.fieldtype, field.options or ""
		level, masked = int(field.permlevel or 0), int(field.get("mask") or 0)
	if (doctype, fieldname) in DECIMAL_DATA_FIELDS and fieldtype == "Data":
		kind = "number"
	elif fieldtype in TEXT_TYPES:
		kind = "text"
	elif fieldtype in NUMBER_TYPES:
		kind = "number"
	elif fieldtype == "Check":
		kind = "boolean"
	elif fieldtype == "Date":
		kind = "date"
	elif fieldtype == "Datetime":
		kind = "datetime"
	elif allow_table and fieldtype in {"Table", "Table MultiSelect"}:
		kind = "table"
	else:
		raise BusinessRuleError("field type is not supported")
	if fieldtype == "Select":
		options = (options or "").splitlines()
		if len(options) > 128 or any(len(option) > 140 for option in options):
			raise BusinessRuleError("select metadata exceeds limits")
	return {
		"doctype": doctype,
		"fieldtype": fieldtype,
		"kind": kind,
		"options": options,
		"permlevel": level,
		"masked": masked,
	}


def contracts_for_paths(doctype: str, paths: list[str] | set[str]) -> dict[str, dict[str, Any]]:
	if len(paths) > MAX_FIELD_PATHS:
		raise BusinessRuleError("too many referenced fields")
	result = {}
	for path in sorted(paths):
		parts = path.split(".")
		if len(parts) == 1:
			result[path] = _field_contract(doctype, parts[0], allow_table=True)
		elif len(parts) == 2:
			parent = _field_contract(doctype, parts[0], allow_table=True)
			if parent["kind"] != "table" or not frappe.get_meta(parent["options"]).istable:
				raise BusinessRuleError("child DocType required")
			result[path] = _field_contract(parent["options"], parts[1])
		else:
			raise BusinessRuleError("invalid field path")
	if len(canonical_json(result).encode("utf-8")) > 32768:
		raise BusinessRuleError("field contracts exceed limit")
	return result


def build_rule_contracts(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
	if set(dimension_fields_for(payload["target_doctype"])) != {"company", "hospital", "organization_unit"}:
		raise BusinessRuleError("reviewed organization dimensions required")
	meta = frappe.get_meta(payload["target_doctype"])
	if (
		meta.istable
		or meta.issingle
		or payload["target_doctype"] in {"HRP Business Rule", "HRP Rule Execution Log"}
	):
		raise BusinessRuleError("unsupported target DocType")
	when = normalize_tree(payload["when_json"], optional=True)
	assertion = normalize_tree(payload["assertion_json"])
	contracts = contracts_for_paths(meta.name, field_paths(when) | field_paths(assertion))
	validate_tree_contract(when, contracts)
	validate_tree_contract(assertion, contracts)
	return contracts


def can_read_fields(
	source: Document, contracts: dict[str, dict[str, Any]], *, user: str | None = None
) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	roles = set(frappe.get_roles(user))
	permissions = [
		p
		for p in frappe.get_meta(source.doctype).permissions
		if p.role in roles and (not p.if_owner or source.owner == user)
	]
	for contract in contracts.values():
		level = contract["permlevel"]
		# Source DocPerm was checked by the caller. Sharing can grant base-level read,
		# but never grants higher field levels or unmask rights. Owner restrictions remain live.
		if level and not any(int(p.permlevel or 0) == level and p.read for p in permissions):
			return False
		if contract["masked"] and not any(
			int(p.permlevel or 0) == level and p.get("mask") for p in permissions
		):
			return False
	return True


def require_field_read(
	source: Document, contracts: dict[str, dict[str, Any]], *, user: str | None = None
) -> None:
	if not can_read_fields(source, contracts, user=user):
		raise_ione_error("PERMISSION_DENIED")


def can_read_named_fields(source: Document, fields: Collection[str], *, user: str | None = None) -> bool:
	"""Check named fields without imposing business-rule scalar type restrictions."""
	contracts: dict[str, dict[str, Any]] = {}
	for path in fields:
		parts = path.split(".")
		meta = frappe.get_meta(source.doctype)
		if len(parts) > 2:
			return False
		for index, fieldname in enumerate(parts):
			field = meta.get_field(fieldname)
			if not field:
				if len(parts) == 1 and fieldname in {
					"name",
					"owner",
					"creation",
					"modified",
					"modified_by",
					"docstatus",
				}:
					continue
				return False
			contracts[f"{path}:{index}"] = {
				"permlevel": int(field.permlevel or 0),
				"masked": int(field.get("mask") or 0),
			}
			if index < len(parts) - 1:
				if field.fieldtype not in {"Table", "Table MultiSelect"}:
					return False
				meta = frappe.get_meta(field.options)
	return can_read_fields(source, contracts, user=user)


def load_contracts(value: str) -> dict[str, dict[str, Any]]:
	try:
		loaded = json.loads(value)
		if not isinstance(loaded, dict):
			raise ValueError("object required")
		return loaded
	except ValueError as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)
