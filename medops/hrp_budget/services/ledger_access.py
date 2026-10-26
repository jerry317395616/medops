"""Original-leaf visibility, separate from private complete-batch integrity."""

from __future__ import annotations

import frappe
from frappe.utils import get_time

from medops.common.budget_ledger import LEDGER, LEDGER_PUBLIC_FIELDS, RELEASE
from medops.common.budget_statement_access import current_statement_read, native_ledger_base_check
from medops.hrp_budget.services.access_graph import read_graph
from medops.hrp_budget.services.consolidation_access import approved_department
from medops.hrp_budget.services.ledger_integrity import complete_batch
from medops.hrp_budget.services.planning_access import READ_ROLES
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.data_access import scope_allowed
from medops.services.errors import raise_ione_error, require_roles


def statement_actor():
	scope = current_statement_read()
	if scope is None or scope.connection is not frappe.local.db or scope.actor != frappe.session.user:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	require_roles(READ_ROLES)
	return scope.actor


def public_entry(row):
	result = {key: row.get(key) for key in LEDGER_PUBLIC_FIELDS}
	for key in ("period_start", "period_end", "posting_date"):
		result[key] = str(result[key])
	result["posting_time"] = get_time(result["posting_time"]).isoformat()
	for key in ("currency_precision", "amount_schema_version", "period_index", "is_reversal"):
		result[key] = int(result[key])
	return result


def authorize_base_entry(doc, *, export=False):
	actor = statement_actor()
	with native_ledger_base_check():
		allowed = frappe.has_permission(LEDGER, "read", doc=doc, user=actor, print_logs=False)
		if export:
			allowed = allowed and frappe.has_permission(
				LEDGER, "export", doc=doc, user=actor, print_logs=False
			)
	if (
		not allowed
		or not scope_allowed(doc, user=actor, action="export" if export else "read")
		or not can_read_named_fields(doc, set(LEDGER_PUBLIC_FIELDS), user=actor)
	):
		raise_ione_error("PERMISSION_DENIED")
	return doc


@read_graph
def authorize_entry(row, references, *, export=False):
	actor = statement_actor()
	doc = authorize_base_entry(frappe.get_doc(dict(row, doctype=LEDGER)), export=export)
	ref = references.get(row.origin_plan)
	if ref is None:
		raise_ione_error("CONFIGURATION_INVALID")
	actual, snapshot = approved_department(ref.submission, ref.approval_case, user=actor, export=export)
	if actual != ref or snapshot.organization_unit != row.organization_unit:
		raise_ione_error("CONFIGURATION_INVALID")
	return doc


def checked_entry(name, *, export=False):
	statement_actor()
	if not frappe.db.exists(LEDGER, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	probe = frappe.get_doc(LEDGER, name)
	authorize_base_entry(probe, export=export)
	if probe.voucher_type != RELEASE or not probe.voucher_no:
		raise_ione_error("CONFIGURATION_INVALID")
	parent = frappe.get_doc(RELEASE, probe.voucher_no)
	facts, rows, _ = complete_batch(parent)
	row = next((item for item in rows if item.name == name), None)
	if row is None:
		raise_ione_error("CONFIGURATION_INVALID")
	authorize_entry(row, {ref.origin_plan: ref for ref in facts.snapshot.sources}, export=export)
	return public_entry(row)
