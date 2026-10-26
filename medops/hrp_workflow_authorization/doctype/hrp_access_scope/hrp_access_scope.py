from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import frappe
from frappe.model.document import Document

from medops.common.access_scope import (
	ACCESS_SCOPE_LEVELS,
	AccessScopeContractError,
	AccessScopeMember,
	build_access_scope_definition,
	build_access_scope_member,
)
from medops.common.organization import OrganizationContractError, normalize_code
from medops.services.errors import raise_ione_error


class HRPAccessScope(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		from medops.hrp_workflow_authorization.doctype.hrp_access_scope_member.hrp_access_scope_member import (
			HRPAccessScopeMember,
		)

		code: DF.Data
		company: DF.Link
		display_name: DF.Data
		enabled: DF.Check
		hospital: DF.Link | None
		include_descendants: DF.Check
		members: DF.Table[HRPAccessScopeMember]
		organization_unit: DF.Link | None
		policy_digest: DF.Data
		remarks: DF.SmallText | None
		revision: DF.Int
		scope_level: Literal["Company", "Hospital", "Organization Unit"]
		valid_from: DF.Date | None
		valid_to: DF.Date | None
	# end: auto-generated types

	def before_naming(self) -> None:
		# field:code naming runs before validate, and Frappe later copies name
		# back to code. Normalize before either identity can be persisted.
		try:
			self.code = normalize_code(self.code, label="code")
		except OrganizationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)

	def validate(self) -> None:
		before = self.get_doc_before_save()
		if before is not None and before.code != self.code:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		try:
			definition = build_access_scope_definition(
				code=self.code,
				display_name=self.display_name,
				company=self.company,
				hospital=self.hospital,
				organization_unit=self.organization_unit,
				include_descendants=self.include_descendants,
				enabled=self.enabled,
				valid_from=self.valid_from,
				valid_to=self.valid_to,
				revision=self.revision or 1,
				remarks=self.remarks,
				members=tuple(self._member_contract(row) for row in self.members),
			)
		except AccessScopeContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)

		# Preserve pre-existing field-based identities, including mixed-case
		# records created before before_naming normalized them. Do not rename
		# access grants as a side effect of disabling or editing their policy.
		self.code = before.code if before is not None else definition.code
		self.display_name = definition.display_name
		self.company = definition.company
		self.hospital = definition.hospital
		self.organization_unit = definition.organization_unit
		self.include_descendants = int(definition.include_descendants)
		self.enabled = int(definition.enabled)
		self.valid_from = definition.valid_from
		self.valid_to = definition.valid_to
		self.remarks = definition.remarks
		self.scope_level = definition.scope_level
		self.policy_digest = definition.policy_digest

		self._validate_references()

	def before_save(self) -> None:
		if self.is_new():
			self.revision = 1
			return
		before = self.get_doc_before_save()
		if before is not None and self._policy_changed(before):
			self.revision = int(before.revision or 1) + 1

	def _member_contract(self, row: HRPAccessScopeMember) -> AccessScopeMember:
		return build_access_scope_member(
			user=row.user,
			target_doctype=row.target_doctype,
			allow_read=row.allow_read,
			allow_create=row.allow_create,
			allow_write=row.allow_write,
			allow_submit=row.allow_submit,
			allow_cancel=row.allow_cancel,
			allow_export=row.allow_export,
			enabled=row.enabled,
		)

	def _validate_references(self) -> None:
		if not frappe.db.exists("Company", self.company):
			raise_ione_error("RESOURCE_NOT_FOUND")
		if self.hospital:
			hospital = frappe.db.get_value(
				"HRP Hospital", self.hospital, ["company", "enabled"], as_dict=True
			)
			if not hospital:
				raise_ione_error("RESOURCE_NOT_FOUND")
			if hospital.company != self.company:
				raise_ione_error("CONFLICT")
			if not bool(hospital.enabled):
				raise_ione_error("INVALID_STATE_TRANSITION")
		if self.organization_unit:
			unit = frappe.db.get_value(
				"HRP Organization Unit",
				self.organization_unit,
				["company", "hospital", "enabled", "organization_version", "lft", "rgt"],
				as_dict=True,
			)
			if not unit:
				raise_ione_error("RESOURCE_NOT_FOUND")
			if unit.company != self.company or unit.hospital != self.hospital:
				raise_ione_error("CONFLICT")
			version = frappe.db.get_value(
				"HRP Organization Version",
				unit.organization_version,
				["docstatus", "status"],
				as_dict=True,
			)
			if (
				not bool(unit.enabled)
				or not version
				or int(version.docstatus) != 1
				or version.status != "Published"
			):
				raise_ione_error("INVALID_STATE_TRANSITION")
			if bool(self.include_descendants) and (unit.lft is None or unit.rgt is None):
				raise_ione_error("CONFIGURATION_INVALID")
		for member in self.members:
			if not frappe.db.exists("User", member.user):
				raise_ione_error("RESOURCE_NOT_FOUND")
			if member.target_doctype and not frappe.db.exists("DocType", member.target_doctype):
				raise_ione_error("RESOURCE_NOT_FOUND")

	def _policy_changed(self, before: Document) -> bool:
		fields = (
			"display_name",
			"company",
			"hospital",
			"organization_unit",
			"include_descendants",
			"enabled",
			"valid_from",
			"valid_to",
			"remarks",
			"policy_digest",
		)
		return any(before.get(fieldname) != self.get(fieldname) for fieldname in fields)

	def as_public_dict(self) -> dict[str, object]:
		members = tuple(self._member_contract(row) for row in self.members)
		definition = build_access_scope_definition(
			code=self.code,
			display_name=self.display_name,
			company=self.company,
			hospital=self.hospital,
			organization_unit=self.organization_unit,
			include_descendants=self.include_descendants,
			enabled=self.enabled,
			valid_from=self.valid_from,
			valid_to=self.valid_to,
			revision=self.revision,
			remarks=self.remarks,
			members=members,
		)
		return definition.as_public_dict()


__all__ = ["ACCESS_SCOPE_LEVELS", "HRPAccessScope"]
