from __future__ import annotations

from typing import TYPE_CHECKING

from medops.hrp_budget.services.approval_documents import BudgetReviewDocument


class HRPBudgetReview(BudgetReviewDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	if TYPE_CHECKING:
		from frappe.types import DF

		action: str
		actor: DF.Link
		case: DF.Link
		case_revision: DF.Int
		company: DF.Link
		confirmation: DF.Link | None
		confirmation_hash: DF.Data
		decision_digest: DF.Data
		event_digest: DF.Data
		event_kind: str
		expires_at: DF.Datetime
		hospital: DF.Link
		organization_unit: DF.Link
		policy_digest: DF.Data
		reason: DF.SmallText
		recorded_at: DF.Datetime
		revision_key: DF.Data | None
		sequence_no: DF.Int
		session_digest: DF.Data
		sod_digest: DF.Data
		source_digest: DF.Data
		vote_key: DF.Data | None
	# end: auto-generated types
