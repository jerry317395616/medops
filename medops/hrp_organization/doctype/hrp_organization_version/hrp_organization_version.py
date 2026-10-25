from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe.model.document import Document

from medops.common.organization import (
	OrganizationContractError,
	normalize_hierarchy_digest,
	normalize_positive_integer,
	normalize_required_date,
	normalize_required_text,
)
from medops.services.errors import raise_ione_error


class HRPOrganizationVersion(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		company: DF.Link
		effective_from: DF.Date
		hierarchy_digest: DF.Data
		hospital: DF.Link
		node_count: DF.Int
		published_at: DF.Datetime | None
		remarks: DF.SmallText | None
		revision: DF.Int
		status: DF.Data
		version_code: DF.Data
		version_label: DF.Data
		version_number: DF.Int
	# end: auto-generated types

	def validate(self) -> None:
		try:
			version_number = normalize_positive_integer(
				self.version_number,
				label="version_number",
			)
			revision = normalize_positive_integer(self.revision or 1, label="revision")
			version_code = normalize_required_text(
				self.version_code,
				label="version_code",
			)
			version_label = normalize_required_text(
				self.version_label,
				label="version_label",
			)
			effective_from = normalize_required_date(
				self.effective_from,
				label="effective_from",
			)
			digest = normalize_hierarchy_digest(self.hierarchy_digest)
		except OrganizationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)

		hospital = frappe.db.get_value(
			"HRP Hospital",
			self.hospital,
			["company", "enabled"],
			as_dict=True,
		)
		if not hospital:
			raise_ione_error("RESOURCE_NOT_FOUND")
		if hospital.company != self.company:
			raise_ione_error("CONFLICT")
		if self.status not in {"Draft", "Published"}:
			raise_ione_error("INVALID_REQUEST")
		if self.docstatus == 0 and self.status != "Draft":
			raise_ione_error("INVALID_STATE_TRANSITION")
		if self.docstatus == 1 and self.status != "Published":
			raise_ione_error("INVALID_STATE_TRANSITION")
		if self.docstatus == 2:
			raise_ione_error("OPERATION_NOT_ALLOWED")

		before = self.get_doc_before_save()
		if before is not None:
			for fieldname in ("hospital", "company", "version_number", "version_code"):
				if before.get(fieldname) != self.get(fieldname):
					raise_ione_error("OPERATION_NOT_ALLOWED")
			if before.docstatus == 1:
				raise_ione_error("OPERATION_NOT_ALLOWED")
			if (
				int(before.node_count or 0) != int(self.node_count or 0) or before.hierarchy_digest != digest
			) and not getattr(self.flags, "organization_service_write", False):
				raise_ione_error("OPERATION_NOT_ALLOWED")

		self.version_number = version_number
		self.revision = revision
		self.version_code = version_code
		self.version_label = version_label
		self.effective_from = effective_from
		self.hierarchy_digest = digest
		self.node_count = int(self.node_count or 0)

	def before_save(self) -> None:
		if self.is_new():
			self.revision = 1
			return
		locked_revision = getattr(self.flags, "locked_revision", None)
		if locked_revision is None:
			locked_revision = self.lock_revision(self.revision, self.name)
		self.revision = locked_revision + 1

	def before_submit(self) -> None:
		if not getattr(self.flags, "organization_service_write", False):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if int(self.node_count or 0) < 1:
			raise_ione_error("INVALID_STATE_TRANSITION")

	def before_cancel(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def validate_update_after_submit(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def on_update_after_submit(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")

	@staticmethod
	def lock_revision(expected_revision: object, name: str) -> int:
		try:
			expected = normalize_positive_integer(
				expected_revision,
				label="expected_revision",
			)
		except OrganizationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		rows = frappe.db.sql(
			"""
			SELECT revision
			FROM `tabHRP Organization Version`
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
		except OrganizationContractError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		if current != expected:
			raise_ione_error("CONFLICT")
		return current

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": 1,
			"name": self.name,
			"hospital": self.hospital,
			"company": self.company,
			"version_number": int(self.version_number),
			"version_code": self.version_code,
			"version_label": self.version_label,
			"effective_from": str(self.effective_from),
			"status": self.status,
			"docstatus": int(self.docstatus),
			"node_count": int(self.node_count or 0),
			"hierarchy_digest": self.hierarchy_digest,
			"revision": int(self.revision),
			"published_at": str(self.published_at) if self.published_at else None,
			"remarks": self.remarks or None,
		}
