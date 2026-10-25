from __future__ import annotations

from typing import TYPE_CHECKING

from frappe.model.document import Document

from medops.common.core_reports import history_write_allowed
from medops.common.domain_service import canonical_json
from medops.common.master_data_security import evaluation_fields
from medops.services.errors import raise_ione_error


class HRPDataQualityEvent(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	if TYPE_CHECKING:
		from frappe.types import DF

		access_fields: DF.Code
		company: DF.Link | None
		event_key: DF.Data
		event_type: str
		from_status: str | None
		hospital: DF.Link | None
		issue: DF.Link | None
		issue_revision: DF.Int
		occurred_at: DF.Datetime
		occurrence_count: DF.Int
		organization_unit: DF.Link | None
		to_status: str | None
	# end: auto-generated types

	def validate(self):
		if not history_write_allowed() or not self.is_new():
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if self.event_type == "Epoch":
			from medops.hrp_master_data.services.quality_history import EPOCH_KEY

			if self.event_key != EPOCH_KEY or self.issue:
				raise_ione_error("CONFIGURATION_INVALID")
			return
		import frappe

		issue = frappe.get_doc("HRP Data Quality Issue", self.issue)
		if (
			any(self.get(k) != issue.get(k) for k in ("company", "hospital", "organization_unit"))
			or int(self.issue_revision) != int(issue.revision)
			or self.to_status != issue.issue_status
			or int(self.occurrence_count) != int(issue.occurrence_count)
		):
			raise_ione_error("CONFIGURATION_INVALID")
		try:
			self.access_fields = canonical_json(evaluation_fields(self.access_fields, issue.target_doctype))
		except ValueError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)

	def on_trash(self):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_rename(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")
