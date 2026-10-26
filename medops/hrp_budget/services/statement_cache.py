"""Private memoization restricted to one phase of an owned read-only graph."""

from __future__ import annotations

import sys
from collections.abc import Callable
from copy import deepcopy
from hashlib import sha256
from typing import TypeVar

import frappe

from medops.common.budget_approval import SUBMISSION
from medops.common.budget_ledger import RELEASE, RELEASE_ENTRY
from medops.common.budget_planning import PLAN, VERSION
from medops.common.budget_statement_access import current_statement_read
from medops.common.budget_workflow import CASE, REVIEW
from medops.hrp_budget.services.access_graph import access_graph_cache, graph_value

T = TypeVar("T")
_DOCUMENT_TYPES = frozenset({VERSION, PLAN, SUBMISSION, CASE, REVIEW, RELEASE, RELEASE_ENTRY})
MAX_DOCUMENT_CACHE_BYTES = 8 * 1024 * 1024
MAX_CACHED_DOCUMENT_ROWS = 2000


def _snapshot_size(value, limit):
	"""Conservative bounded weight; shared values may be counted more than once."""
	stack = [value]
	total = 0
	while stack:
		item = stack.pop()
		total += sys.getsizeof(item)
		if total > limit:
			return None
		if isinstance(item, dict):
			stack.extend(item.keys())
			stack.extend(item.values())
		elif isinstance(item, (list, tuple)):
			stack.extend(item)
	return total


def statement_document_key(doc) -> str:
	# Bind the full supplied document without retaining another serialized copy
	# of private source snapshots in the graph's key set.
	return sha256(frappe.as_json(doc.as_dict()).encode("utf-8")).hexdigest()


def cached_statement_value(key: tuple | Callable[[], tuple], reader: Callable[[], T]) -> T:
	scope = current_statement_read()
	if scope is None or scope.connection is not frappe.local.db or scope.actor != frappe.session.user:
		return reader()
	# The connection adapter already enforces RC/READ ONLY. Each initial/final
	# collection enters a different graph; neither authority nor source success
	# survives the final refresh, another actor, a write, or another request.
	return graph_value(
		("statement_value", id(scope.connection), scope.actor, key() if callable(key) else key), reader
	)


def read_statement_document(doctype: str, name: str, *, for_update=False):
	"""Reuse stored source data in one read phase; never reuse mutable documents.

	This grants no visibility. Every caller still performs the native, field,
	scope and complete-source checks. Only already reviewed budget source types
	participate; physical ledgers and arbitrary framework documents do not.
	"""
	scope = current_statement_read()
	graph = access_graph_cache()
	if (
		for_update
		or not isinstance(doctype, str)
		or not isinstance(name, str)
		or doctype not in _DOCUMENT_TYPES
		or scope is None
		or scope.connection is not frappe.local.db
		or scope.actor != frappe.session.user
		or graph is None
	):
		return frappe.get_doc(doctype, name, for_update=for_update)
	key = ("source_document", id(scope.connection), scope.actor, doctype, name)
	if key in graph:
		return frappe.get_doc(deepcopy(graph[key]))
	doc = frappe.get_doc(doctype, name)
	if (
		sum(len(doc.get(field.fieldname) or []) for field in doc.meta.get_table_fields())
		> MAX_CACHED_DOCUMENT_ROWS
	):
		return doc
	budget = graph.setdefault(("source_document_budget",), {"used": 0})
	snapshot = doc.as_dict()
	weight = _snapshot_size(snapshot, MAX_DOCUMENT_CACHE_BYTES - budget["used"])
	if weight is None:
		return doc
	# A controller/permission caller can add flags or change its input object.
	# Only detached stored data is cached; later calls receive a fresh native
	# controller with its own child documents and cannot poison that snapshot.
	graph[key] = deepcopy(snapshot)
	budget["used"] += weight
	return doc
