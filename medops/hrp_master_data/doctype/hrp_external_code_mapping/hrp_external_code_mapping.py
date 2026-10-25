from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe.model.document import Document

from medops.common.external_code_mapping import (
	ExternalCodeMappingContractError,
	normalize_external_code,
	scope_key_for,
	source_key_for,
	target_key_for,
)
from medops.common.master_data import MasterDataContractError, get_target_policy
from medops.common.organization import (
	OrganizationContractError,
	normalize_boolean,
	normalize_code,
	normalize_optional_date,
	normalize_optional_text,
	normalize_positive_integer,
	normalize_reference,
	normalize_required_date,
	normalize_required_text,
	validate_date_range,
)
from medops.services.errors import raise_ione_error


class HRPExternalCodeMapping(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		company: DF.Link
		enabled: DF.Check
		external_code: DF.Data
		external_label: DF.Data | None
		external_system: DF.Data
		hospital: DF.Link
		internal_name: DF.DynamicLink
		master_data_domain: DF.Link
		organization_unit: DF.Link | None
		remarks: DF.SmallText | None
		revision: DF.Int
		scope_key: DF.Data
		source_key: DF.Data
		target_key: DF.Data
		target_doctype: DF.Link
		valid_from: DF.Date
		valid_to: DF.Date | None
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
			self.organization_unit = (
				normalize_reference(self.organization_unit, label="organization_unit")
				if self.organization_unit
				else None
			)
			self.scope_key = scope_key_for(self.organization_unit)
			self.external_system = normalize_code(
				self.external_system,
				label="external_system",
			)
			self.external_code = normalize_external_code(self.external_code)
			self.external_label = (
				normalize_required_text(
					self.external_label,
					label="external_label",
					maximum=140,
				)
				if self.external_label
				else None
			)
			self.internal_name = normalize_reference(self.internal_name, label="internal_name")
			self.source_key = source_key_for(
				master_data_domain=self.master_data_domain,
				company=self.company,
				hospital=self.hospital,
				scope_key=self.scope_key,
				external_system=self.external_system,
				external_code=self.external_code,
			)
			self.target_key = target_key_for(
				master_data_domain=self.master_data_domain,
				company=self.company,
				hospital=self.hospital,
				scope_key=self.scope_key,
				external_system=self.external_system,
				internal_name=self.internal_name,
			)
			self.enabled = int(normalize_boolean(self.enabled, label="enabled"))
			self.valid_from = normalize_required_date(self.valid_from, label="valid_from")
			self.valid_to = normalize_optional_date(self.valid_to, label="valid_to")
			validate_date_range(str(self.valid_from), str(self.valid_to) if self.valid_to else None)
			self.revision = normalize_positive_integer(self.revision or 1, label="revision")
			self.remarks = normalize_optional_text(self.remarks, label="remarks")
		except (
			ExternalCodeMappingContractError,
			MasterDataContractError,
			OrganizationContractError,
		) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		if policy.target_doctype != self.target_doctype:
			raise_ione_error("CONFIGURATION_INVALID")

		before = self.get_doc_before_save()
		if before is not None:
			for fieldname in (
				"master_data_domain",
				"target_doctype",
				"company",
				"hospital",
				"organization_unit",
				"scope_key",
				"source_key",
				"external_system",
				"external_code",
			):
				if before.get(fieldname) != self.get(fieldname):
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
			getattr(self.flags, "external_code_mapping_service_write", False)
			or getattr(self.flags, "master_data_migration", False)
		):
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
			FROM `tabHRP External Code Mapping`
			WHERE name = %s
			FOR UPDATE
			""",
			name,
			as_dict=True,
		)
		if not rows:
			raise_ione_error("RESOURCE_NOT_FOUND")
		current = int(rows[0].revision or 0)
		if current != expected:
			raise_ione_error("CONFLICT")
		return current

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": 1,
			"name": self.name,
			"master_data_domain": self.master_data_domain,
			"target_doctype": self.target_doctype,
			"internal_name": self.internal_name,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit or None,
			"external_system": self.external_system,
			"external_code": self.external_code,
			"external_label": self.external_label or None,
			"enabled": bool(self.enabled),
			"valid_from": str(self.valid_from),
			"valid_to": str(self.valid_to) if self.valid_to else None,
			"revision": int(self.revision),
			"remarks": self.remarks or None,
		}
