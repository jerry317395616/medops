from __future__ import annotations

from typing import TYPE_CHECKING

from medops.hrp_budget.services.submission_documents import BudgetSubmissionDocument


class HRPBudgetSubmission(BudgetSubmissionDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	if TYPE_CHECKING:
		from frappe.types import DF

		applicant: DF.Link
		company: DF.Link
		cost_center: DF.Link
		currency: DF.Link
		currency_precision: DF.Int
		fiscal_year: DF.Link
		hospital: DF.Link
		identity_key: DF.Data
		lines: DF.Table
		organization_unit: DF.Link
		organization_version: DF.Link
		posting_date: DF.Date
		remarks: DF.SmallText | None
		revision: DF.Int
		schema_version: DF.Int
		sealed_at: DF.Datetime
		sealed_by: DF.Link
		snapshot_digest: DF.Data
		source_plan: DF.Link
		source_plan_digest: DF.Data
		source_plan_revision: DF.Int
		source_version: DF.Link
		source_version_digest: DF.Data
		status: str
		subject: DF.Data
		submission_type: str
		total_amount: DF.Data
	# end: auto-generated types
