from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

import frappe
from frappe.model.document import Document

from medops.common.numbering import (
	RESET_POLICIES,
	NumberingContractError,
	build_numbering_scheme_upsert,
)
from medops.common.organization import normalize_positive_integer
from medops.services.errors import raise_ione_error


class HRPNumberingScheme(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		code: DF.Data
		dimension_keys: DF.Code
		display_name: DF.Data
		enabled: DF.Check
		company: DF.Link
		hospital: DF.Link
		organization_unit: DF.Link | None
		remarks: DF.SmallText | None
		reset_policy: Literal["Never", "Yearly", "Monthly", "Daily"]
		revision: DF.Int
		sequence_digits: DF.Int
		start_number: DF.Int
		template: DF.Data
		template_digest: DF.Data
		valid_from: DF.Date | None
		valid_to: DF.Date | None
	# end: auto-generated types

	def before_insert(self) -> None:
		self._require_service_write()

	def validate(self) -> None:
		self._require_service_write()
		try:
			command = build_numbering_scheme_upsert(
				scheme_name=self.name if not self.is_new() else None,
				code=self.code,
				display_name=self.display_name,
				company=self.company,
				hospital=self.hospital,
				organization_unit=self.organization_unit,
				template=self.template,
				reset_policy=self.reset_policy,
				sequence_digits=self.sequence_digits,
				start_number=self.start_number,
				enabled=self.enabled,
				valid_from=self.valid_from,
				valid_to=self.valid_to,
				expected_revision=0,
				remarks=self.remarks,
			)
		except NumberingContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)

		self.code = command.code
		self.display_name = command.display_name
		self.company = command.company
		self.hospital = command.hospital
		self.organization_unit = command.organization_unit
		self.template = command.template
		self.dimension_keys = json.dumps(
			command.dimension_keys,
			ensure_ascii=False,
			separators=(",", ":"),
		)
		self.reset_policy = command.reset_policy
		self.sequence_digits = command.sequence_digits
		self.start_number = command.start_number
		self.template_digest = command.template_digest
		self.enabled = int(command.enabled)
		self.valid_from = command.valid_from
		self.valid_to = command.valid_to
		self.remarks = command.remarks

		before = self.get_doc_before_save()
		if before is None:
			return
		for fieldname in ("code", "company", "hospital", "organization_unit"):
			if before.get(fieldname) != self.get(fieldname):
				raise_ione_error("OPERATION_NOT_ALLOWED")
		format_fields = ("template", "reset_policy", "sequence_digits", "start_number")
		if any(before.get(fieldname) != self.get(fieldname) for fieldname in format_fields) and (
			frappe.db.exists("HRP Number Reservation", {"numbering_scheme": self.name})
		):
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

	def on_trash(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def _require_service_write(self) -> None:
		if not (
			getattr(self.flags, "numbering_service_write", False)
			or getattr(self.flags, "numbering_migration", False)
		):
			raise_ione_error("OPERATION_NOT_ALLOWED")

	@staticmethod
	def lock_revision(expected_revision: object, name: str) -> int:
		try:
			expected = normalize_positive_integer(expected_revision, label="expected_revision")
		except ValueError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		rows = frappe.db.sql(
			"""
			SELECT revision
			FROM `tabHRP Numbering Scheme`
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

	def get_dimension_keys(self) -> tuple[str, ...]:
		try:
			loaded = json.loads(self.dimension_keys or "[]")
		except json.JSONDecodeError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		if not isinstance(loaded, list) or any(not isinstance(key, str) for key in loaded):
			raise_ione_error("CONFIGURATION_INVALID")
		return tuple(loaded)

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": 1,
			"name": self.name,
			"code": self.code,
			"display_name": self.display_name,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit or None,
			"template": self.template,
			"dimension_keys": self.get_dimension_keys(),
			"reset_policy": self.reset_policy,
			"sequence_digits": int(self.sequence_digits),
			"start_number": int(self.start_number),
			"enabled": bool(self.enabled),
			"valid_from": str(self.valid_from) if self.valid_from else None,
			"valid_to": str(self.valid_to) if self.valid_to else None,
			"revision": int(self.revision),
			"remarks": self.remarks or None,
		}


__all__ = ["RESET_POLICIES", "HRPNumberingScheme"]
