from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from frappe.model.document import Document

from medops.common.data_quality import ISSUE_STATUSES, SEVERITIES, issue_key_for
from medops.common.domain_service import canonical_json, normalize_sha256
from medops.common.master_data_security import evaluation_fields
from medops.common.organization import (
	OrganizationContractError,
	normalize_code,
	normalize_positive_integer,
	normalize_reference,
	normalize_required_text,
)
from medops.services.errors import raise_ione_error


class HRPDataQualityIssue(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		company: DF.Link
		evaluation_read_fields: DF.Code | None
		failure_code: DF.Data | None
		failure_message: DF.Data | None
		first_detected_at: DF.Datetime
		hospital: DF.Link
		issue_key: DF.Data
		issue_status: Literal["Open", "Resolved"]
		last_evaluated_at: DF.Datetime
		master_data_domain: DF.Link
		observed_value_digest: DF.Data
		occurrence_count: DF.Int
		organization_unit: DF.Link | None
		quality_rule: DF.Link
		resolved_at: DF.Datetime | None
		revision: DF.Int
		rule_digest: DF.Data
		rule_revision: DF.Int
		severity: Literal["Critical", "Major", "Minor"]
		target_doctype: DF.Link
		target_name: DF.DynamicLink
	# end: auto-generated types

	def before_insert(self) -> None:
		self._require_service_write()

	def validate(self) -> None:
		self._require_service_write()
		try:
			self.quality_rule = normalize_reference(self.quality_rule, label="quality_rule")
			self.master_data_domain = normalize_code(
				self.master_data_domain,
				label="master_data_domain",
			)
			self.target_doctype = normalize_reference(self.target_doctype, label="target_doctype")
			self.target_name = normalize_reference(self.target_name, label="target_name")
			self.company = normalize_reference(self.company, label="company")
			self.hospital = normalize_code(self.hospital, label="hospital")
			self.organization_unit = (
				normalize_reference(self.organization_unit, label="organization_unit")
				if self.organization_unit
				else None
			)
			self.issue_key = normalize_sha256(self.issue_key, label="issue_key")
			if self.issue_status not in ISSUE_STATUSES:
				raise OrganizationContractError("issue_status is invalid")
			if self.severity not in SEVERITIES:
				raise OrganizationContractError("severity is invalid")
			self.failure_code = (
				normalize_code(self.failure_code, label="failure_code") if self.failure_code else None
			)
			self.failure_message = (
				normalize_required_text(
					self.failure_message,
					label="failure_message",
					maximum=140,
				)
				if self.failure_message
				else None
			)
			self.observed_value_digest = normalize_sha256(
				self.observed_value_digest,
				label="observed_value_digest",
			)
			self.rule_revision = normalize_positive_integer(
				self.rule_revision,
				label="rule_revision",
			)
			self.rule_digest = normalize_sha256(self.rule_digest, label="rule_digest")
			self.occurrence_count = normalize_positive_integer(
				self.occurrence_count,
				label="occurrence_count",
			)
			self.revision = normalize_positive_integer(self.revision or 1, label="revision")
		except (OrganizationContractError, ValueError) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		if self.issue_key != issue_key_for(
			rule_name=self.quality_rule,
			target_doctype=self.target_doctype,
			target_name=self.target_name,
		):
			raise_ione_error("CONFIGURATION_INVALID")
		if self.issue_status == "Open" and not self.failure_code:
			raise_ione_error("CONFIGURATION_INVALID")
		if self.issue_status == "Resolved" and not self.resolved_at:
			raise_ione_error("CONFIGURATION_INVALID")

		before = self.get_doc_before_save()
		try:
			fields = evaluation_fields(self.get("evaluation_read_fields"), self.target_doctype)
			if (
				before
				and before.get("evaluation_read_fields")
				and not set(evaluation_fields(before.evaluation_read_fields, self.target_doctype)).issubset(
					fields
				)
			):
				raise_ione_error("OPERATION_NOT_ALLOWED")
			self.evaluation_read_fields = canonical_json(fields)
		except ValueError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		if before is not None:
			for fieldname in (
				"quality_rule",
				"master_data_domain",
				"target_doctype",
				"target_name",
				"company",
				"hospital",
				"organization_unit",
				"issue_key",
				"first_detected_at",
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

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": 1,
			"name": self.name,
			"quality_rule": self.quality_rule,
			"master_data_domain": self.master_data_domain,
			"target_doctype": self.target_doctype,
			"target_name": self.target_name,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit or None,
			"issue_status": self.issue_status,
			"severity": self.severity,
			"failure_code": self.failure_code or None,
			"failure_message": self.failure_message or None,
			"rule_revision": int(self.rule_revision),
			"first_detected_at": str(self.first_detected_at),
			"last_evaluated_at": str(self.last_evaluated_at),
			"resolved_at": str(self.resolved_at) if self.resolved_at else None,
			"occurrence_count": int(self.occurrence_count),
			"revision": int(self.revision),
		}
