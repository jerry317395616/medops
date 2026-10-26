from __future__ import annotations

import frappe

from medops.common.budget_statement_access import current_statement_read
from medops.hrp_budget.services.access_graph import access_graph_cache
from medops.hrp_budget.services.release_execution import refresh_financial_authority
from medops.services.errors import raise_ione_error


def refresh_statement_actor():
	scope = current_statement_read()
	if scope is None or scope.connection is not frappe.local.db:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	if (graph := access_graph_cache()) is not None:
		# An explicit refresh is a new authority phase even if a caller still
		# holds an outer graph context. Drop stored data and permission results.
		graph.clear()
	state = frappe.db.get_value("User", scope.actor, ["enabled", "user_type"], as_dict=True)
	if not state or not state.enabled or state.user_type != "System User":
		raise_ione_error("PERMISSION_DENIED")
	refresh_financial_authority(scope.actor)
	if not bool(frappe.db.sql("SELECT @@tx_read_only")[0][0]):
		raise_ione_error("CONFIGURATION_INVALID")
	return scope.actor
