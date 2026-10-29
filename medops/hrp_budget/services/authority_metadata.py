"""Fresh primary-transaction metadata batches, never permission-result caching.

The pinned native string constructor loads DocType and its five child tables
separately for every name. Group only those raw reads, then run native Meta
processing unchanged. Unknown constructors retain the native fresh read path.
"""

from __future__ import annotations

import hashlib
import inspect
import textwrap
from functools import lru_cache

import frappe
from frappe.model import meta as native_meta
from frappe.model.base_document import TABLE_DOCTYPES_FOR_DOCTYPE, BaseDocument
from frappe.model.document import Document

MAX_METADATA_TYPES = 256
TABLES = (
	("fields", "DocField"),
	("permissions", "DocPerm"),
	("actions", "DocType Action"),
	("links", "DocType Link"),
	("states", "DocType State"),
)
CHILD_FIELDS = frozenset(field for field, _child in TABLES)
# Normalized source from owner fork 4880ad14d2b067e8db1a8356ef2d325713cc3cf4.
# Only code compatibility is memoized. No metadata, actor, role or allow result is.
CONSTRUCTOR_HASHES = (
	"57733c355dbb780a262eeec21b8e72fb5c89497b77669f6607aa2941e994896c",
	"126c6c163e39fb77bb4eed048fb6a093baa834d7a191a3e838495a076c4a55b8",
	"77005818bfeba5adfdd34be03849bb33610fddc4081fa67a90ce35b30b589617",
	"a541e2bbdbbb18fc58bb007b8193a7355886d87d22658d34421e50f38f5040d9",
	"631632cabace98f473bbe52ef58962f9ea13d4b1c17d746d5bbe8e1e3b372d09",
	"5c07ac2c4f7d733d0694fbf670a2957d8fd4557388db2fd0638e71f730a72bd3",
	"a662cc7d3eaa44630da59c03c7a48b7f2ce75b853600196cb267f50cb4c2e0d5",
	"d9ed88427a88f2070ba2f342cbec575d6fcec88402762759e664396fb8d1efc8",
)


@lru_cache(maxsize=8)
def _compatible_code(codes, expected_hashes):
	try:
		return all(
			hashlib.sha256(textwrap.dedent(inspect.getsource(code)).strip().encode()).hexdigest() == expected
			for code, expected in zip(codes, expected_hashes, strict=True)
		)
	except (OSError, TypeError, ValueError):
		return False


def supported_native_constructor():
	"""Check current callable identities too, so a live override cannot be bypassed."""
	if (
		frappe.get_meta is not native_meta.get_meta
		or native_meta.Meta.__module__ != "frappe.model.meta"
		or native_meta.Meta.__qualname__ != "Meta"
		or native_meta.Meta.__new__ is not object.__new__
		or tuple(TABLE_DOCTYPES_FOR_DOCTYPE.items()) != TABLES
		or hasattr(native_meta.Meta, "__setup__")
	):
		return False
	functions = (
		native_meta.get_meta,
		native_meta.Meta.__init__,
		native_meta.Meta.load_from_db,
		native_meta.Meta.process,
		Document.__init__,
		Document.load_from_db,
		Document.load_children_from_db,
		BaseDocument.__init__,
	)
	if not all(
		inspect.isfunction(function) and not hasattr(function, "__wrapped__") for function in functions
	):
		return False
	# Inspect executed code directly; inspecting a function can unwrap a decorator
	# and approve an overridden reader. Store no function closures in this cache.
	return _compatible_code(tuple(function.__code__ for function in functions), CONSTRUCTOR_HASHES)


def fresh_metadata_batch(names):
	"""Construct current metadata; callers still own RC, identity, rights and guards.

	Every invocation reads current rows on frappe.db, including its uncommitted
	changes. It neither grants access nor supplies a reusable authorization lease.
	"""
	if isinstance(names, str):
		raise ValueError("metadata name collection required")
	names = tuple(names)
	if not names or any(not isinstance(name, str) or not name or name.strip() != name for name in names):
		raise ValueError("bounded canonical metadata names required")
	names = tuple(sorted(set(names)))
	if len(names) > MAX_METADATA_TYPES:
		raise ValueError("bounded canonical metadata names required")
	if not supported_native_constructor():
		return {name: frappe.get_meta(name, cached=False) for name in names}
	rows = frappe.db.get_values("DocType", {"name": ("in", names)}, "*", as_dict=True, order_by=None)
	documents = {row.name: dict(row) for row in rows or []}
	if set(documents) != set(names):
		# Preserve native special-DocType fallback and missing-DocType errors.
		return {name: frappe.get_meta(name, cached=False) for name in names}
	for field, child in TABLES:
		for document in documents.values():
			document[field] = []
		for row in (
			frappe.db.get_values(
				child,
				{"parent": ("in", names), "parenttype": "DocType", "parentfield": field},
				"*",
				as_dict=True,
				order_by="parent asc, idx asc",
			)
			or []
		):
			documents[row.parent][field].append(dict(row))
	result = {}
	for name in names:
		# Match the pinned Meta(str) -> Document.load_from_db -> BaseDocument
		# state. Meta(Document) instead reserializes config and changes fields.
		meta = native_meta.Meta.__new__(native_meta.Meta)
		meta.doctype = "DocType"
		meta.name = name
		meta.flags = frappe._dict(for_update=False, ignore_children=True)
		BaseDocument.__init__(
			meta, {key: value for key, value in documents[name].items() if key not in CHILD_FIELDS}
		)
		meta.flags.pop("ignore_children", None)
		for field, _child in TABLES:
			meta.set(field, documents[name][field])
		meta.process()
		frappe.client_cache.set_value(f"doctype_meta::{name}", meta)
		result[name] = meta
	return result
