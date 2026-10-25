from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe.model.document import Document

from medops.common.organization import (
	UNIT_TYPES,
	OrganizationContractError,
	normalize_code,
	normalize_positive_integer,
)
from medops.common.organization_mapping import (
	OrganizationMappingContractError,
	build_organization_mapping_upsert,
)
from medops.services.errors import raise_ione_error


class HRPOrganizationMapping(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		company: DF.Link
		cost_center: DF.Link | None
		department: DF.Link | None
		enabled: DF.Check
		hospital: DF.Link
		organization_unit: DF.Link
		organization_version: DF.Link
		remarks: DF.SmallText | None
		revision: DF.Int
		unit_code: DF.Data
		unit_type: DF.Data
	# end: auto-generated types

	def validate(self) -> None:
		if not getattr(self.flags, "organization_mapping_service_write", False):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		try:
			command = build_organization_mapping_upsert(
				organization_version=self.organization_version,
				organization_unit=self.organization_unit,
				department=self.department,
				cost_center=self.cost_center,
				enabled=self.enabled,
				expected_revision=0 if self.is_new() else self.revision,
				remarks=self.remarks,
			)
			unit_code = normalize_code(self.unit_code, label="unit_code")
			revision = normalize_positive_integer(self.revision or 1, label="revision")
		except (OrganizationContractError, OrganizationMappingContractError) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		if self.unit_type not in UNIT_TYPES:
			raise_ione_error("INVALID_REQUEST")

		version = frappe.db.get_value(
			"HRP Organization Version",
			command.organization_version,
			["company", "hospital", "docstatus", "status"],
			as_dict=True,
		)
		if not version:
			raise_ione_error("RESOURCE_NOT_FOUND")
		if int(version.docstatus) != 1 or version.status != "Published":
			raise_ione_error("INVALID_STATE_TRANSITION")
		unit = frappe.db.get_value(
			"HRP Organization Unit",
			command.organization_unit,
			["organization_version", "company", "hospital", "code", "unit_type"],
			as_dict=True,
		)
		if not unit:
			raise_ione_error("RESOURCE_NOT_FOUND")
		if (
			unit.organization_version != command.organization_version
			or unit.company != version.company
			or unit.hospital != version.hospital
			or self.company != version.company
			or self.hospital != version.hospital
			or unit.code != unit_code
			or unit.unit_type != self.unit_type
		):
			raise_ione_error("CONFLICT")

		before = self.get_doc_before_save()
		if before is not None:
			for fieldname in (
				"organization_version",
				"organization_unit",
				"company",
				"hospital",
				"unit_code",
				"unit_type",
			):
				if before.get(fieldname) != self.get(fieldname):
					raise_ione_error("OPERATION_NOT_ALLOWED")

		self.department = command.department
		self.cost_center = command.cost_center
		self.enabled = int(command.enabled)
		self.remarks = command.remarks
		self.unit_code = unit_code
		self.revision = revision

	def before_save(self) -> None:
		if self.is_new():
			self.revision = 1
			return
		locked_revision = getattr(self.flags, "locked_revision", None)
		if locked_revision is None:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		self.revision = int(locked_revision) + 1

	def on_trash(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": 1,
			"name": self.name,
			"organization_version": self.organization_version,
			"organization_unit": self.organization_unit,
			"company": self.company,
			"hospital": self.hospital,
			"unit_code": self.unit_code,
			"unit_type": self.unit_type,
			"department": self.department or None,
			"cost_center": self.cost_center or None,
			"enabled": bool(self.enabled),
			"revision": int(self.revision or 1),
			"remarks": self.remarks or None,
		}
