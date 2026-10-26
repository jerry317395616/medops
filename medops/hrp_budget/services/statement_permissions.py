"""Private authority fingerprint; never exposed in statement hashes or logs."""

from __future__ import annotations

import json
from hashlib import sha256

import frappe

from medops.common.domain_service import canonical_json
from medops.hrp_budget.services.ledger_access import statement_actor
from medops.hrp_budget.services.release_execution import AUTHORITY_ROOTS
from medops.services.errors import raise_ione_error

MAX_AUTHORITY_ROWS = 50000


def statement_metadata_types():
	actor = statement_actor()
	pending = (
		set(AUTHORITY_ROOTS)
		| {
			"User",
			"Has Role",
			"Role",
			"User Permission",
			"DocShare",
			"HRP Access Scope",
			"HRP Access Scope Member",
		}
		| set(frappe.get_all("User Permission", filters={"user": actor}, pluck="allow"))
	)
	seen = set()
	while pending:
		name = pending.pop()
		if name in seen:
			continue
		seen.add(name)
		if len(seen) > 256:
			raise_ione_error("CONFIGURATION_INVALID")
		meta = frappe.get_meta(name)
		pending.update(
			field.options
			for field in meta.fields
			if field.fieldtype in {"Table", "Table MultiSelect"}
			and field.options
			and field.options not in seen
		)
	return tuple(sorted(seen))


def statement_permission_stamp(metadata_types):
	actor = statement_actor()
	digest = sha256()
	count = 0

	def add(doctype, *, filters=None, or_filters=None, fields=None):
		nonlocal count
		rows = frappe.get_all(
			doctype,
			filters=filters,
			or_filters=or_filters,
			fields=fields or ["*"],
			order_by="name asc",
			limit_page_length=MAX_AUTHORITY_ROWS + 1,
		)
		count += len(rows)
		if count > MAX_AUTHORITY_ROWS:
			raise_ione_error("CONFIGURATION_INVALID")
		digest.update(doctype.encode())
		for row in rows:
			value = canonical_json(json.loads(frappe.as_json(row))).encode()
			digest.update(len(value).to_bytes(8, "big"))
			digest.update(value)

	add("User", filters={"name": actor}, fields=["name", "enabled", "user_type"])
	add("Has Role", filters={"parenttype": "User", "parent": actor})
	add("Role")
	add("User Permission", filters={"user": actor})
	add("DocShare", or_filters={"user": actor, "everyone": 1})
	members = frappe.get_all(
		"HRP Access Scope Member",
		filters={"user": actor},
		fields=["parent"],
		limit_page_length=MAX_AUTHORITY_ROWS + 1,
	)
	if len(members) > MAX_AUTHORITY_ROWS:
		raise_ione_error("CONFIGURATION_INVALID")
	add("HRP Access Scope Member", filters={"user": actor})
	if members:
		add("HRP Access Scope", filters={"name": ["in", sorted({row.parent for row in members})]})
	for doctype in ("DocType", "DocField", "DocPerm", "Custom DocPerm"):
		add(doctype, filters={"name" if doctype == "DocType" else "parent": ["in", list(metadata_types)]})
	for doctype in ("Custom Field", "Property Setter"):
		add(
			doctype, filters={"dt" if doctype == "Custom Field" else "doc_type": ["in", list(metadata_types)]}
		)
	return digest.hexdigest()
