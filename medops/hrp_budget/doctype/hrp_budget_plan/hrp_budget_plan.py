from __future__ import annotations

from typing import TYPE_CHECKING

from medops.services.budget_documents import BudgetPlanDocument


class HRPBudgetPlan(BudgetPlanDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	if TYPE_CHECKING:
		from frappe.types import DF

		applicant: DF.Link
		budget_version: DF.Link
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
		source_plan: DF.Link | None
		status: str
		subject: DF.Data
		total_amount: DF.Data
	# end: auto-generated types
