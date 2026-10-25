from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import frappe
from frappe.model.document import Document

from medops.common.data_quality import (
	DataQualityContractError,
	build_data_quality_rule_upsert,
	validate_rule_for_policy,
)
from medops.common.domain_service import normalize_sha256
from medops.common.master_data import MasterDataContractError, get_target_policy
from medops.common.organization import OrganizationContractError, normalize_positive_integer
from medops.services.errors import raise_ione_error


class HRPDataQualityRule(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		code: DF.Data
		company: DF.Link
		display_name: DF.Data
		enabled: DF.Check
		hospital: DF.Link
		master_data_domain: DF.Link
		organization_unit: DF.Link | None
		parameters_json: DF.Code
		remarks: DF.SmallText | None
		revision: DF.Int
		rule_digest: DF.Data
		rule_type: Literal[
			"Required", "Allowed Values", "Maximum Length", "Named Pattern", "Reference Exists"
		]
		severity: Literal["Critical", "Major", "Minor"]
		target_doctype: DF.Link
		target_field: DF.Data
		valid_from: DF.Date
		valid_to: DF.Date | None
	# end: auto-generated types

	def before_insert(self) -> None:
		self._require_service_write()

	def validate(self) -> None:
		self._require_service_write()
		try:
			command = build_data_quality_rule_upsert(
				rule_name=None if self.is_new() else self.name,
				code=self.code,
				display_name=self.display_name,
				master_data_domain=self.master_data_domain,
				company=self.company,
				hospital=self.hospital,
				organization_unit=self.organization_unit,
				target_field=self.target_field,
				rule_type=self.rule_type,
				parameters=self.parameters_json,
				severity=self.severity,
				enabled=self.enabled,
				valid_from=self.valid_from,
				valid_to=self.valid_to,
				expected_revision=0 if self.is_new() else self.revision,
				remarks=self.remarks,
			)
			policy = get_target_policy(self.target_doctype)
			validate_rule_for_policy(command, policy)
			self.rule_digest = normalize_sha256(command.rule_digest, label="rule_digest")
		except (
			DataQualityContractError,
			MasterDataContractError,
			OrganizationContractError,
		) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		if policy.target_doctype != self.target_doctype:
			raise_ione_error("CONFIGURATION_INVALID")
		self.code = command.code
		self.display_name = command.display_name
		self.master_data_domain = command.master_data_domain
		self.company = command.company
		self.hospital = command.hospital
		self.organization_unit = command.organization_unit
		self.target_field = command.target_field
		self.rule_type = command.rule_type
		self.parameters_json = command.parameters_json
		self.severity = command.severity
		self.enabled = int(command.enabled)
		self.valid_from = command.valid_from
		self.valid_to = command.valid_to
		self.remarks = command.remarks

		before = self.get_doc_before_save()
		if before is not None:
			for fieldname in (
				"code",
				"master_data_domain",
				"target_doctype",
				"company",
				"hospital",
				"organization_unit",
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
			getattr(self.flags, "data_quality_service_write", False)
			or getattr(self.flags, "master_data_migration", False)
		):
			raise_ione_error("OPERATION_NOT_ALLOWED")

	@staticmethod
	def lock_revision(expected_revision: object, name: str) -> int:
		try:
			expected = normalize_positive_integer(expected_revision, label="expected_revision")
		except OrganizationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		rows = frappe.db.sql(
			"""
			SELECT revision
			FROM `tabHRP Data Quality Rule`
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
			"code": self.code,
			"display_name": self.display_name,
			"master_data_domain": self.master_data_domain,
			"target_doctype": self.target_doctype,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit or None,
			"target_field": self.target_field,
			"rule_type": self.rule_type,
			"parameters": frappe.parse_json(self.parameters_json),
			"severity": self.severity,
			"enabled": bool(self.enabled),
			"valid_from": str(self.valid_from),
			"valid_to": str(self.valid_to) if self.valid_to else None,
			"rule_digest": self.rule_digest,
			"revision": int(self.revision),
			"remarks": self.remarks or None,
		}
