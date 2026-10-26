from __future__ import annotations

from typing import TYPE_CHECKING

from medops.hrp_budget.services.approval_documents import BudgetApprovalDocument


class HRPBudgetApproval(BudgetApprovalDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	if TYPE_CHECKING:
		from frappe.types import DF

		active_key: DF.Data | None
		applicant: DF.Link
		company: DF.Link
		completed_at: DF.Datetime | None
		cost_center: DF.Link
		currency: DF.Link
		currency_precision: DF.Int
		current_step: DF.Int
		decision_digest: DF.Data
		decision_snapshot: DF.Code
		final_review: DF.Link | None
		fiscal_year: DF.Link
		hospital: DF.Link
		identity_key: DF.Data
		organization_unit: DF.Link
		organization_version: DF.Link
		policy_digest: DF.Data
		policy_matrix: DF.Link
		policy_revision: DF.Int
		posting_date: DF.Date
		requested_at: DF.Datetime
		requested_by: DF.Link
		revision: DF.Int
		round_number: DF.Int
		source_applicant: DF.Link
		source_digest: DF.Data
		status: str
		subject: DF.Data
		submission: DF.Link
		total_amount: DF.Data
		workflow_state: DF.Link
	# end: auto-generated types
