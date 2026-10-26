from __future__ import annotations

from typing import TYPE_CHECKING

from medops.hrp_budget.services.release_documents import BudgetLedgerDocument


class HRPBudgetLedger(BudgetLedgerDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	if TYPE_CHECKING:
		from frappe.types import DF

		account: DF.Link
		amount: DF.Data
		amount_schema_version: DF.Int
		budget_subject: DF.Link
		company: DF.Link
		cost_center: DF.Link
		credit: DF.Data
		currency: DF.Link
		currency_precision: DF.Int
		debit: DF.Data
		description: DF.SmallText | None
		dimensions_json: DF.Code | None
		entry_key: DF.Data
		entry_type: str
		fact_key: DF.Data
		fiscal_year: DF.Link
		funding_source: DF.Link
		hospital: DF.Link
		is_reversal: DF.Check
		organization_unit: DF.Link
		organization_version: DF.Link
		origin_line_key: DF.Data
		origin_plan: DF.Link
		origin_plan_digest: DF.Data
		origin_version: DF.Link
		origin_version_digest: DF.Data
		period_end: DF.Date
		period_index: DF.Int
		period_start: DF.Date
		posting_date: DF.Date
		posting_time: DF.Time
		project: DF.Link | None
		quantity: DF.Float | None
		reference_name: DF.DynamicLink | None
		reference_type: DF.Link | None
		release_digest: DF.Data
		reversal_of: DF.Link | None
		source_approval: DF.Link
		source_decision_digest: DF.Data
		source_event_digest: DF.Data
		source_final_review: DF.Link
		source_hash: DF.Data
		source_snapshot_digest: DF.Data
		source_submission: DF.Link
		voucher_no: DF.DynamicLink
		voucher_type: DF.Link
	# end: auto-generated types
