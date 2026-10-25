from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from frappe.model.document import Document


class HRPApprovalMatrixRow(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		approval_mode: Literal["All", "Any"]
		approver_role: DF.Link | None
		approver_type: Literal["Role", "User"]
		approver_user: DF.Link | None
		description: DF.SmallText | None
		sequence_no: DF.Int
		step_name: DF.Data
		threshold_amount: DF.Currency
	# end: auto-generated types


__all__ = ["HRPApprovalMatrixRow"]
