from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe.utils.nestedset import NestedSet

from medops.common.organization import (
	OrganizationContractError,
	normalize_boolean,
	normalize_code,
	normalize_optional_date,
	normalize_optional_text,
	normalize_positive_integer,
	normalize_required_text,
	validate_date_range,
)
from medops.services.errors import raise_ione_error


class HRPOrganizationUnit(NestedSet):
	nsm_parent_field = "parent_organization_unit"

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		code: DF.Data
		company: DF.Link
		display_name: DF.Data
		enabled: DF.Check
		hospital: DF.Link
		is_group: DF.Check
		lft: DF.Int
		old_parent: DF.Data | None
		organization_version: DF.Link
		parent_organization_unit: DF.Link | None
		remarks: DF.SmallText | None
		rgt: DF.Int
		sequence: DF.Int
		unit_type: DF.Data
		valid_from: DF.Date | None
		valid_to: DF.Date | None
	# end: auto-generated types

	def autoname(self) -> None:
		try:
			code = normalize_code(self.code, label="code")
		except OrganizationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		self.name = f"{self.organization_version}-{code}"
		if len(self.name) > 140:
			raise_ione_error("INVALID_REQUEST")

	def validate(self) -> None:
		if not getattr(self.flags, "organization_service_write", False):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		try:
			code = normalize_code(self.code, label="code")
			display_name = normalize_required_text(
				self.display_name,
				label="display_name",
			)
			enabled = normalize_boolean(self.enabled, label="enabled")
			is_group = normalize_boolean(self.is_group, label="is_group")
			sequence = normalize_positive_integer(self.sequence, label="sequence")
			valid_from = normalize_optional_date(self.valid_from, label="valid_from")
			valid_to = normalize_optional_date(self.valid_to, label="valid_to")
			validate_date_range(valid_from, valid_to)
			remarks = normalize_optional_text(self.remarks, label="remarks")
		except OrganizationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)

		version = frappe.db.get_value(
			"HRP Organization Version",
			self.organization_version,
			["company", "hospital", "docstatus", "status"],
			as_dict=True,
		)
		if not version:
			raise_ione_error("RESOURCE_NOT_FOUND")
		if int(version.docstatus) != 0 or version.status != "Draft":
			raise_ione_error("INVALID_STATE_TRANSITION")
		if self.company != version.company or self.hospital != version.hospital:
			raise_ione_error("CONFLICT")
		if self.parent_organization_unit:
			parent = frappe.db.get_value(
				"HRP Organization Unit",
				self.parent_organization_unit,
				["organization_version", "is_group"],
				as_dict=True,
			)
			if (
				not parent
				or parent.organization_version != self.organization_version
				or not bool(parent.is_group)
			):
				raise_ione_error("CONFLICT")

		self.code = code
		self.display_name = display_name
		self.enabled = int(enabled)
		self.is_group = int(is_group)
		self.sequence = sequence
		self.valid_from = valid_from
		self.valid_to = valid_to
		self.remarks = remarks

	def on_trash(self) -> None:
		if not getattr(self.flags, "organization_service_write", False):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		version = frappe.db.get_value(
			"HRP Organization Version",
			self.organization_version,
			["docstatus", "status"],
			as_dict=True,
		)
		if not version or int(version.docstatus) != 0 or version.status != "Draft":
			raise_ione_error("INVALID_STATE_TRANSITION")
