"""Private complete-batch proof. Its output never grants source visibility."""

from __future__ import annotations

import frappe
from frappe.utils import get_time

from medops.common.budget_consolidation import MAX_CONSOLIDATED_LINES
from medops.common.budget_ledger import LEDGER, RELEASE, allocation_values, reversal_allocation_values
from medops.hrp_budget.services.release_documents import same_value
from medops.hrp_budget.services.release_sources import stored_release_facts
from medops.services.errors import raise_ione_error


def complete_batch(parent, *, facts=None):
	if parent.status != "Posted" or int(parent.docstatus) != 1:
		raise_ione_error("INVALID_STATE_TRANSITION")
	facts = facts or stored_release_facts(parent)
	rows = frappe.get_all(
		LEDGER,
		filters={"voucher_type": RELEASE, "voucher_no": parent.name},
		fields=["*"],
		order_by="entry_key asc",
		limit_page_length=MAX_CONSOLIDATED_LINES + 1,
	)
	if not rows or len(rows) != len(facts.snapshot.lines) or len(rows) != int(parent.ledger_count):
		raise_ione_error("CONFIGURATION_INVALID")
	by_name = {row.name: row for row in rows}
	if len(by_name) != len(rows):
		raise_ione_error("CONFIGURATION_INVALID")
	clock = get_time(rows[0].posting_time).isoformat()
	if facts.operation == "Release":
		expected = (
			dict(values, is_reversal=0, reversal_of=None)
			for values in allocation_values(facts, posting_time=clock)
		)
	else:
		original = frappe.get_doc(RELEASE, facts.reversal_of)
		original_facts, original_rows, original_clock = complete_batch(original)
		del original_rows
		expected = reversal_allocation_values(
			original_facts, facts, original_posting_time=original_clock, posting_time=clock
		)
	meta = frappe.get_meta(LEDGER)
	matched = set()
	for values in expected:
		row = by_name.get(values["entry_key"])
		if (
			row is None
			or row.name != row.entry_key
			or any(
				not same_value(field, row.get(field.fieldname), values.get(field.fieldname))
				for field in meta.fields
				if field.fieldtype not in {"Section Break", "Column Break", "Tab Break"}
			)
		):
			raise_ione_error("CONFIGURATION_INVALID")
		matched.add(row.name)
	if matched != set(by_name):
		raise_ione_error("CONFIGURATION_INVALID")
	return facts, rows, clock
