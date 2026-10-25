from __future__ import annotations

from typing import TYPE_CHECKING

from medops.services.budget_documents import BudgetVersionDocument


class HRPBudgetVersion(BudgetVersionDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	if TYPE_CHECKING:
		from frappe.types import DF

		cloned_from: DF.Link | None
		company: DF.Link
		currency: DF.Link
		currency_precision: DF.Int
		distribution_frequency: str
		fiscal_year: DF.Link
		frozen_at: DF.Datetime | None
		frozen_by: DF.Link | None
		hospital: DF.Link
		organization_version: DF.Link
		period_end: DF.Date
		period_start: DF.Date
		remarks: DF.SmallText | None
		revision: DF.Int
		snapshot_digest: DF.Data | None
		status: str
		subject: DF.Data
		version_number: DF.Int
	# end: auto-generated types
