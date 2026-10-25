from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe.model.document import Document

from medops.common.master_data import (
	MasterDataContractError,
	get_target_policy,
)
from medops.common.organization import (
	normalize_boolean,
	normalize_code,
	normalize_optional_text,
	normalize_positive_integer,
	normalize_required_text,
)
from medops.services.errors import raise_ione_error


class HRPMasterDataDomain(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		allow_create: DF.Check
		allow_disable: DF.Check
		allow_update: DF.Check
		allowed_fields: DF.SmallText
		code: DF.Data
		display_name: DF.Data
		enabled: DF.Check
		policy_digest: DF.Data
		policy_version: DF.Int
		remarks: DF.SmallText | None
		revision: DF.Int
		target_doctype: DF.Link
	# end: auto-generated types

	def before_insert(self) -> None:
		self._require_service_write()

	def validate(self) -> None:
		self._require_service_write()
		try:
			policy = get_target_policy(self.target_doctype)
			self.code = normalize_code(self.code, label="code")
			self.display_name = normalize_required_text(
				self.display_name,
				label="display_name",
			)
			self.enabled = int(normalize_boolean(self.enabled, label="enabled"))
			self.remarks = normalize_optional_text(self.remarks, label="remarks")
			self.revision = normalize_positive_integer(self.revision or 1, label="revision")
		except (MasterDataContractError, ValueError) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)

		if not frappe.db.exists("DocType", policy.target_doctype):
			raise_ione_error("CONFIGURATION_INVALID")
		before = self.get_doc_before_save()
		if before is not None:
			if before.code != self.code or before.target_doctype != policy.target_doctype:
				raise_ione_error("OPERATION_NOT_ALLOWED")
		self.target_doctype = policy.target_doctype
		self.allow_create = int(policy.allow_create)
		self.allow_update = int(policy.allow_update)
		self.allow_disable = int(policy.allow_disable)
		self.allowed_fields = "\n".join(field.field_name for field in policy.fields)
		self.policy_version = policy.version
		self.policy_digest = policy.digest

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
			getattr(self.flags, "master_data_service_write", False)
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
		except ValueError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		rows = frappe.db.sql(
			"""
			SELECT revision
			FROM `tabHRP Master Data Domain`
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
			"code": self.code,
			"display_name": self.display_name,
			"target_doctype": self.target_doctype,
			"enabled": bool(self.enabled),
			"allow_create": bool(self.allow_create),
			"allow_update": bool(self.allow_update),
			"allow_disable": bool(self.allow_disable),
			"allowed_fields": tuple(field for field in (self.allowed_fields or "").splitlines() if field),
			"policy_version": int(self.policy_version),
			"policy_digest": self.policy_digest,
			"revision": int(self.revision),
			"remarks": self.remarks or None,
		}
