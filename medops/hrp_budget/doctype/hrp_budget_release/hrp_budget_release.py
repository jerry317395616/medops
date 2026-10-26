from __future__ import annotations

from typing import TYPE_CHECKING

from medops.hrp_budget.services.release_documents import BudgetReleaseDocument


class HRPBudgetRelease(BudgetReleaseDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	if TYPE_CHECKING:
		from frappe.types import DF

		amount_schema_version: DF.Int
		authorization_decision: DF.Link | None
		authorization_revision: DF.Int
		authorized_at: DF.Datetime | None
		authorized_by: DF.Link | None
		company: DF.Link
		currency: DF.Link
		currency_precision: DF.Int
		execution_digest: DF.Data | None
		fiscal_year: DF.Link
		hospital: DF.Link
		identity_key: DF.Data
		intent_digest: DF.Data
		ledger_count: DF.Int
		line_count: DF.Int
		operation: str
		organization_unit: DF.Link
		organization_version: DF.Link
		original_intent_digest: DF.Data | None
		outbox_message: DF.Link | None
		posted_at: DF.Datetime | None
		posting_date: DF.Date
		prepared_at: DF.Datetime
		prepared_by: DF.Link
		reason: DF.SmallText
		reversal_of: DF.Link | None
		revision: DF.Int
		schema_version: DF.Int
		source_approval: DF.Link
		source_approval_revision: DF.Int
		source_approval_round: DF.Int
		source_decision_digest: DF.Data
		source_event_digest: DF.Data
		source_final_review: DF.Link
		source_snapshot_digest: DF.Data
		source_submission: DF.Link
		status: str
		subject: DF.Data
		total_amount: DF.Data
	# end: auto-generated types
