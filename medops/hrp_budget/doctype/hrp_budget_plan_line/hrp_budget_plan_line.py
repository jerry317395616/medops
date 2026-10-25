from __future__ import annotations

from typing import TYPE_CHECKING

from medops.services.budget_documents import BudgetPlanLineDocument


class HRPBudgetPlanLine(BudgetPlanLineDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	if TYPE_CHECKING:
		from frappe.types import DF

		account: DF.Link
		amount: DF.Data
		budget_subject: DF.Link
		description: DF.SmallText | None
		funding_source: DF.Link
		line_key: DF.Data
		period_end: DF.Date
		period_index: DF.Int
		period_start: DF.Date
		project: DF.Link | None
	# end: auto-generated types
