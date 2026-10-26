from __future__ import annotations

from typing import TYPE_CHECKING

from medops.hrp_budget.services.release_documents import BudgetReleaseEntryDocument


class HRPBudgetReleaseEntry(BudgetReleaseEntryDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	if TYPE_CHECKING:
		from frappe.types import DF

		action: str
		actor: DF.Link
		company: DF.Link
		confirmation: DF.Link | None
		confirmation_hash: DF.Data
		event_digest: DF.Data
		event_kind: str
		expires_at: DF.Datetime
		hospital: DF.Link
		intent_digest: DF.Data
		organization_unit: DF.Link
		reason: DF.SmallText
		recorded_at: DF.Datetime
		release: DF.Link
		release_revision: DF.Int
		revision_key: DF.Data | None
		session_digest: DF.Data
		source_snapshot_digest: DF.Data
	# end: auto-generated types
