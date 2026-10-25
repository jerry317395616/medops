from __future__ import annotations

from typing import TYPE_CHECKING

from medops.services.budget_documents import BudgetMasterDocument


class HRPDimensionValue(BudgetMasterDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	if TYPE_CHECKING:
		from frappe.types import DF

		code: DF.Data
		company: DF.Link
		dimension: DF.Link
		display_name: DF.Data
		enabled: DF.Check | None
		hospital: DF.Link
		identity_key: DF.Data
		remarks: DF.SmallText | None
		revision: DF.Int
		valid_from: DF.Date
		valid_to: DF.Date | None
	# end: auto-generated types
