"""Owned outer HTTP transaction boundary for experimental reservation evidence."""

from __future__ import annotations

from collections import deque

import frappe

from medops.hrp_budget.services.access_graph import access_graph_cache
from medops.hrp_budget.services.approval import require_web_session
from medops.hrp_budget.services.release_execution import require_current_read_transaction
from medops.services.errors import raise_ione_error


def prepare_reservation_http_transaction():
	"""Restart only a clean, unnested POST before any domain service runs.

	Frappe owns final commit or rollback. Pending writes, transaction callbacks,
	budget guards or read graphs make the request fail closed rather than discard
	other work. This boundary does not itself grant access or reserve funds.
	"""
	require_web_session()
	db = frappe.local.db
	if (
		db.db_type != "mariadb"
		or db.transaction_writes
		or db.auto_commit_on_many_writes
		or db._disable_transaction_control
		or getattr(frappe.local, "job", None)
		or getattr(frappe.local, "_medops_reservation_http_started", False)
		or getattr(db, "_medops_budget_guard_marker", None) is not None
		or access_graph_cache() is not None
	):
		raise_ione_error("OPERATION_NOT_ALLOWED")
	for name in ("before_commit", "after_commit", "before_rollback", "after_rollback"):
		queue = getattr(getattr(db, name, None), "_functions", None)
		if not isinstance(queue, deque) or queue:
			raise_ione_error("OPERATION_NOT_ALLOWED")
	actor, sid, form = frappe.session.user, frappe.session.sid, frappe.local.form_dict
	frappe.local._medops_reservation_http_started = True
	db.rollback()
	db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
	db.begin()
	if frappe.session.user != actor or frappe.session.sid != sid or frappe.local.form_dict is not form:
		raise_ione_error("CONFLICT")
	require_current_read_transaction()
