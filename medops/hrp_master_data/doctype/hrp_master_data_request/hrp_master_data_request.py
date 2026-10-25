from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import frappe
from frappe.model.document import Document

from medops.common.master_data import (
	MasterDataContractError,
	get_target_policy,
)
from medops.common.organization import (
	normalize_code,
	normalize_optional_text,
	normalize_positive_integer,
	normalize_reference,
	normalize_required_date,
	normalize_required_text,
)
from medops.services.errors import raise_ione_error


class HRPMasterDataRequest(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		from medops.hrp_master_data.doctype.hrp_master_data_change_item.hrp_master_data_change_item import (
			HRPMasterDataChangeItem,
		)

		baseline_modified_at: DF.Datetime | None
		changes: DF.Table[HRPMasterDataChangeItem]
		company: DF.Link
		decision_reason: DF.SmallText | None
		effective_on: DF.Date
		hospital: DF.Link
		master_data_domain: DF.Link
		operation: Literal["Create", "Update", "Disable"]
		organization_unit: DF.Link
		proposal_digest: DF.Data
		request_status: Literal["Draft", "Pending Review", "Approved", "Rejected"]
		requested_at: DF.Datetime
		requested_by: DF.Link
		reviewed_at: DF.Datetime | None
		reviewed_by: DF.Link | None
		revision: DF.Int
		subject: DF.Data
		submitted_at: DF.Datetime | None
		target_doctype: DF.Link
		target_name: DF.DynamicLink | None
	# end: auto-generated types

	def before_insert(self) -> None:
		self._require_service_write()

	def validate(self) -> None:
		self._require_service_write()
		try:
			self.master_data_domain = normalize_code(
				self.master_data_domain,
				label="master_data_domain",
			)
			policy = get_target_policy(self.target_doctype)
			self.company = normalize_reference(self.company, label="company")
			self.hospital = normalize_code(self.hospital, label="hospital")
			self.organization_unit = normalize_reference(
				self.organization_unit,
				label="organization_unit",
			)
			self.subject = normalize_required_text(self.subject, label="subject")
			self.effective_on = normalize_required_date(
				self.effective_on,
				label="effective_on",
			)
			self.requested_by = normalize_reference(
				self.requested_by,
				label="requested_by",
			)
			self.revision = normalize_positive_integer(self.revision or 1, label="revision")
			self.decision_reason = normalize_optional_text(
				self.decision_reason,
				label="decision_reason",
			)
		except (MasterDataContractError, ValueError) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)

		if self.operation not in {"Create", "Update", "Disable"}:
			raise_ione_error("INVALID_REQUEST")
		if self.request_status not in {"Draft", "Pending Review", "Approved", "Rejected"}:
			raise_ione_error("INVALID_REQUEST")
		if not self.changes:
			raise_ione_error("INVALID_REQUEST")
		if self.operation == "Create" and self.target_name:
			raise_ione_error("INVALID_REQUEST")
		if self.operation != "Create" and not self.target_name:
			raise_ione_error("INVALID_REQUEST")
		if self.docstatus == 0 and self.request_status != "Draft":
			raise_ione_error("INVALID_STATE_TRANSITION")
		if self.docstatus == 1 and self.request_status not in {
			"Pending Review",
			"Approved",
			"Rejected",
		}:
			raise_ione_error("INVALID_STATE_TRANSITION")
		if self.docstatus == 2:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if policy.target_doctype != self.target_doctype:
			raise_ione_error("CONFIGURATION_INVALID")

		before = self.get_doc_before_save()
		if before is not None and before.docstatus == 1:
			for fieldname in (
				"master_data_domain",
				"target_doctype",
				"operation",
				"target_name",
				"subject",
				"company",
				"hospital",
				"organization_unit",
				"effective_on",
				"proposal_digest",
				"baseline_modified_at",
				"requested_by",
				"requested_at",
				"submitted_at",
			):
				if before.get(fieldname) != self.get(fieldname):
					raise_ione_error("OPERATION_NOT_ALLOWED")
			if self._change_snapshot(before.get("changes")) != self._change_snapshot(self.get("changes")):
				raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_save(self) -> None:
		self._require_service_write()
		if self.is_new():
			self.revision = 1
			return
		locked_revision = getattr(self.flags, "locked_revision", None)
		if locked_revision is None:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		self.revision = int(locked_revision) + 1

	def before_submit(self) -> None:
		self._require_service_write()
		if self.request_status != "Pending Review":
			raise_ione_error("INVALID_STATE_TRANSITION")

	def before_cancel(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def validate_update_after_submit(self) -> None:
		self._require_service_write()
		if self.request_status not in {"Approved", "Rejected"}:
			raise_ione_error("INVALID_STATE_TRANSITION")

	def on_update_after_submit(self) -> None:
		self._require_service_write()

	def on_trash(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def _require_service_write(self) -> None:
		if not (
			getattr(self.flags, "master_data_service_write", False)
			or getattr(self.flags, "master_data_migration", False)
		):
			raise_ione_error("OPERATION_NOT_ALLOWED")

	@staticmethod
	def _change_snapshot(rows: object) -> tuple[tuple[object, ...], ...]:
		if not isinstance(rows, list):
			return ()
		return tuple(
			(
				row.get("sequence_no"),
				row.get("field_name"),
				row.get("field_label"),
				row.get("value_type"),
				row.get("current_value"),
				row.get("proposed_value"),
				row.get("reason"),
			)
			for row in rows
		)

	@staticmethod
	def lock_revision(expected_revision: object, name: str) -> int:
		try:
			expected = normalize_positive_integer(
				expected_revision,
				label="expected_revision",
			)
		except ValueError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		rows = frappe.db.sql(
			"""
			SELECT revision
			FROM `tabHRP Master Data Request`
			WHERE name = %s
			FOR UPDATE
			""",
			name,
			as_dict=True,
		)
		if not rows:
			raise_ione_error("RESOURCE_NOT_FOUND")
		try:
			current = normalize_positive_integer(rows[0].revision, label="revision")
		except ValueError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		if current != expected:
			raise_ione_error("CONFLICT")
		return current

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": 1,
			"name": self.name,
			"master_data_domain": self.master_data_domain,
			"target_doctype": self.target_doctype,
			"operation": self.operation,
			"target_name": self.target_name or None,
			"subject": self.subject,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit,
			"effective_on": str(self.effective_on),
			"request_status": self.request_status,
			"docstatus": int(self.docstatus),
			"proposal_digest": self.proposal_digest,
			"baseline_modified_at": (str(self.baseline_modified_at) if self.baseline_modified_at else None),
			"requested_by": self.requested_by,
			"requested_at": str(self.requested_at),
			"submitted_at": str(self.submitted_at) if self.submitted_at else None,
			"reviewed_by": self.reviewed_by or None,
			"reviewed_at": str(self.reviewed_at) if self.reviewed_at else None,
			"decision_reason": self.decision_reason or None,
			"revision": int(self.revision),
			"changes": [
				{
					"sequence_no": int(item.sequence_no),
					"field_name": item.field_name,
					"field_label": item.field_label,
					"value_type": item.value_type,
					"current_value": item.current_value or "",
					"proposed_value": item.proposed_value or "",
					"reason": item.reason or None,
				}
				for item in self.changes
			],
		}
