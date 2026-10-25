from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe.model.document import Document

from medops.common.organization import (
	OrganizationContractError,
	build_hospital_upsert,
	normalize_positive_integer,
)
from medops.services.errors import raise_ione_error


class HRPHospital(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		code: DF.Data
		company: DF.Link
		display_name: DF.Data
		enabled: DF.Check
		next_version_number: DF.Int
		remarks: DF.SmallText | None
		revision: DF.Int
		valid_from: DF.Date | None
		valid_to: DF.Date | None
	# end: auto-generated types

	def validate(self) -> None:
		try:
			command = build_hospital_upsert(
				code=self.code,
				company=self.company,
				display_name=self.display_name,
				enabled=self.enabled,
				valid_from=self.valid_from,
				valid_to=self.valid_to,
				remarks=self.remarks,
				expected_revision=0 if self.is_new() else self.revision,
			)
			next_version_number = normalize_positive_integer(
				self.next_version_number or 1,
				label="next_version_number",
			)
		except OrganizationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		if not frappe.db.exists("Company", command.company):
			raise_ione_error("RESOURCE_NOT_FOUND")

		before = self.get_doc_before_save()
		if before is not None:
			if before.company != command.company and frappe.db.exists(
				"HRP Organization Version",
				{"hospital": self.name},
			):
				raise_ione_error("OPERATION_NOT_ALLOWED")
			if int(before.next_version_number or 1) != next_version_number and not getattr(
				self.flags, "organization_version_allocation", False
			):
				raise_ione_error("OPERATION_NOT_ALLOWED")

		self.code = command.code
		self.company = command.company
		self.display_name = command.display_name
		self.enabled = int(command.enabled)
		self.valid_from = command.valid_from
		self.valid_to = command.valid_to
		self.remarks = command.remarks
		self.next_version_number = next_version_number

	def before_save(self) -> None:
		if self.is_new():
			self.revision = 1
			self.next_version_number = self.next_version_number or 1
			return
		locked_revision = getattr(self.flags, "locked_revision", None)
		if locked_revision is None:
			locked_revision = self.lock_revision(self.revision, self.name)
		self.revision = locked_revision + 1

	@staticmethod
	def lock_revision(expected_revision: object, hospital: str) -> int:
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
			FROM `tabHRP Hospital`
			WHERE name = %s
			FOR UPDATE
			""",
			hospital,
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

	def on_trash(self) -> None:
		if frappe.db.exists("HRP Organization Version", {"hospital": self.name}):
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": 1,
			"name": self.name,
			"code": self.code,
			"company": self.company,
			"display_name": self.display_name,
			"enabled": bool(self.enabled),
			"valid_from": str(self.valid_from) if self.valid_from else None,
			"valid_to": str(self.valid_to) if self.valid_to else None,
			"remarks": self.remarks or None,
			"revision": int(self.revision or 1),
		}
