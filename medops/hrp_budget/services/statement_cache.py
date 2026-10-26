"""Private memoization restricted to one phase of an owned read-only graph."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import frappe

from medops.common.budget_statement_access import current_statement_read
from medops.hrp_budget.services.access_graph import graph_value

T = TypeVar("T")


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
