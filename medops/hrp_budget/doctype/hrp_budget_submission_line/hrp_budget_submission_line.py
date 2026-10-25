from __future__ import annotations

from typing import TYPE_CHECKING

from medops.hrp_budget.services.submission_documents import BudgetSubmissionLineDocument


class HRPBudgetSubmissionLine(BudgetSubmissionLineDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	if TYPE_CHECKING:
		from frappe.types import DF

		account: DF.Link
		amount: DF.Data
		budget_subject: DF.Link
		cost_center: DF.Link
		description: DF.SmallText | None
		fact_key: DF.Data
		funding_source: DF.Link
		organization_unit: DF.Link
		origin_line_key: DF.Data
		origin_plan: DF.Link
		origin_plan_digest: DF.Data
		origin_version: DF.Link
		origin_version_digest: DF.Data
		period_end: DF.Date
		period_index: DF.Int
		period_start: DF.Date
		project: DF.Link | None
	# end: auto-generated types
