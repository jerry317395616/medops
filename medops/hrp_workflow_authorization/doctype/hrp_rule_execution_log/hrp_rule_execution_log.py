from __future__ import annotations

from frappe.model.document import Document

from medops.services.errors import raise_ione_error


class HRPRuleExecutionLog(Document):
	def validate(self) -> None:
		if not self.is_new() or not self.flags.business_rule_log_append:
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def on_trash(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")
