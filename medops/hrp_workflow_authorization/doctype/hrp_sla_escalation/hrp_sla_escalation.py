from __future__ import annotations

import json

from frappe.model.document import Document

from medops.common.sla import sla_digest
from medops.services.errors import raise_ione_error


class HRPSLAEscalation(Document):
	def validate(self) -> None:
		if not self.flags.sla_service_write:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		before = self.get_doc_before_save()
		identity = (
			"cycle_key",
			"reference_doctype",
			"reference_name",
			"source_start_at",
			"sla_policy",
			"policy_revision",
			"policy_digest",
			"snapshot_json",
			"snapshot_digest",
			"company",
			"hospital",
			"organization_unit",
			"started_at",
			"responsible_user",
		)
		if before and any(str(before.get(key) or "") != str(self.get(key) or "") for key in identity):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if self.flags.sla_error_only:
			allowed = {
				"last_error_code",
				"last_error_at",
				"retry_count",
				"next_run_at",
				"revision",
				"modified",
				"modified_by",
			}
			if not before or any(
				before.get(f.fieldname) != self.get(f.fieldname)
				for f in self.meta.fields
				if f.fieldname not in allowed
			):
				raise_ione_error("OPERATION_NOT_ALLOWED")
		else:
			try:
				snapshot = json.loads(self.snapshot_json)
				assignments = json.loads(self.current_assignments_json or "{}")
				if (
					sla_digest(snapshot) != self.snapshot_digest
					or not isinstance(assignments, dict)
					or set(assignments) - {"response", "resolution"}
				):
					raise ValueError("invalid SLA snapshot")
			except (TypeError, ValueError) as exc:
				raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		self.revision = int(before.revision) + 1 if before else 1

	def on_trash(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")
