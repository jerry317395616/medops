from __future__ import annotations

import json
from typing import TYPE_CHECKING

import frappe
from frappe.model.document import Document

from medops.common.approval_matrix import (
	ApprovalMatrixContractError,
	ApprovalMatrixDefinition,
	ApprovalMatrixStep,
	approval_amount_profile,
	build_approval_matrix_definition,
	build_approval_matrix_step,
)
from medops.common.organization import normalize_positive_integer
from medops.hrp_workflow_authorization.services.access_scope import dimension_fields_for
from medops.services.audit_context import emit_audit_event
from medops.services.errors import raise_ione_error


class HRPApprovalMatrix(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		from medops.hrp_workflow_authorization.doctype.hrp_approval_matrix_row.hrp_approval_matrix_row import (
			HRPApprovalMatrixRow,
		)

		code: DF.Data
		company: DF.Link
		dimensions_json: DF.Code | None
		display_name: DF.Data
		enabled: DF.Check
		hospital: DF.Link
		include_descendants: DF.Check
		organization_unit: DF.Link | None
		policy_digest: DF.Data
		priority: DF.Int
		remarks: DF.SmallText | None
		revision: DF.Int
		steps: DF.Table[HRPApprovalMatrixRow]
		target_doctype: DF.Link
		valid_from: DF.Date
		valid_to: DF.Date | None
	# end: auto-generated types

	def validate(self) -> None:
		definition = self.as_definition(revision=self.revision or 1)
		self.set("amount_schema_version", definition.schema_version)
		self.set("amount_precision", definition.amount_precision)
		if definition.schema_version == 2:
			for row, step in zip(self.steps, definition.steps, strict=True):
				for fieldname, value in step.as_dict().items():
					row.set("exact_threshold_amount" if fieldname == "threshold_amount" else fieldname, value)
				row.threshold_amount = 0
		self.code = definition.code
		self.display_name = definition.display_name
		self.target_doctype = definition.target_doctype
		self.company = definition.company
		self.hospital = definition.hospital
		self.organization_unit = definition.organization_unit
		self.include_descendants = int(definition.include_descendants)
		self.priority = definition.priority
		self.dimensions_json = json.dumps(
			definition.dimensions,
			ensure_ascii=False,
			separators=(",", ":"),
			sort_keys=True,
		)
		self.enabled = int(definition.enabled)
		self.valid_from = definition.valid_from
		self.valid_to = definition.valid_to
		self.remarks = definition.remarks
		self.policy_digest = definition.policy_digest
		self._validate_references()

		before = self.get_doc_before_save()
		if before is not None:
			if before.amount_profile() != self.amount_profile():
				raise_ione_error("OPERATION_NOT_ALLOWED")
			for fieldname in (
				"code",
				"target_doctype",
				"company",
				"hospital",
				"organization_unit",
			):
				if before.get(fieldname) != self.get(fieldname):
					raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_save(self) -> None:
		if self.is_new():
			self.revision = 1
			return
		before = self.get_doc_before_save()
		if before is None or before.policy_digest == self.policy_digest:
			return
		locked_revision = getattr(self.flags, "locked_revision", None)
		self.revision = int(locked_revision if locked_revision is not None else before.revision) + 1

	def on_update(self) -> None:
		emit_audit_event(
			"approval_matrix_saved",
			logger_name="medops.approval_matrix",
			policy_digest=self.policy_digest,
			revision=int(self.revision),
			step_count=len(self.steps),
			enabled=bool(self.enabled),
		)

	def on_trash(self) -> None:
		if not getattr(self.flags, "approval_matrix_migration", False):
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def as_definition(self, *, revision: object) -> ApprovalMatrixDefinition:
		try:
			version, precision = self.amount_profile()
			return build_approval_matrix_definition(
				code=self.code,
				display_name=self.display_name,
				target_doctype=self.target_doctype,
				company=self.company,
				hospital=self.hospital,
				organization_unit=self.organization_unit,
				include_descendants=self.include_descendants,
				priority=self.priority,
				dimensions=self.dimensions_json,
				enabled=self.enabled,
				valid_from=self.valid_from,
				valid_to=self.valid_to,
				revision=revision,
				remarks=self.remarks,
				steps=tuple(self._step_contract(row) for row in self.steps),
				schema_version=version,
				amount_precision=precision,
			)
		except ApprovalMatrixContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)

	def amount_profile(self) -> tuple[int, int]:
		return approval_amount_profile(
			self.get("amount_schema_version") if self.get("amount_schema_version") is not None else 1,
			self.get("amount_precision") if self.get("amount_precision") is not None else 2,
		)

	def _step_contract(self, row: HRPApprovalMatrixRow) -> ApprovalMatrixStep:
		version, precision = self.amount_profile()
		exact = row.get("exact_threshold_amount")
		if (version == 1 and exact not in (None, "")) or (
			version == 2 and (exact in (None, "") or row.threshold_amount not in (None, 0, 0.0))
		):
			raise ApprovalMatrixContractError("mixed approval threshold storage versions")
		return build_approval_matrix_step(
			sequence_no=row.sequence_no,
			step_name=row.step_name,
			threshold_amount=row.threshold_amount if version == 1 else exact,
			approver_type=row.approver_type,
			approver_role=row.approver_role,
			approver_user=row.approver_user,
			approval_mode=row.approval_mode,
			description=row.description,
			schema_version=version,
			amount_precision=precision,
		)

	def _validate_references(self) -> None:
		for doctype, name in (
			("Company", self.company),
			("DocType", self.target_doctype),
			("HRP Hospital", self.hospital),
		):
			if not frappe.db.exists(doctype, name):
				raise_ione_error("RESOURCE_NOT_FOUND")
		dimension_fields = dimension_fields_for(self.target_doctype)
		if set(dimension_fields) != {"company", "hospital", "organization_unit"}:
			raise_ione_error("CONFIGURATION_INVALID")
		hospital = frappe.db.get_value(
			"HRP Hospital",
			self.hospital,
			["company", "enabled", "valid_from", "valid_to"],
			as_dict=True,
		)
		if hospital.company != self.company:
			raise_ione_error("CONFLICT")
		if not bool(hospital.enabled):
			raise_ione_error("INVALID_STATE_TRANSITION")
		if hospital.valid_from and str(hospital.valid_from) > str(self.valid_from):
			raise_ione_error("CONFLICT")
		if hospital.valid_to and (not self.valid_to or str(hospital.valid_to) < str(self.valid_to)):
			raise_ione_error("CONFLICT")
		if self.organization_unit:
			unit = frappe.db.get_value(
				"HRP Organization Unit",
				self.organization_unit,
				[
					"company",
					"hospital",
					"enabled",
					"organization_version",
					"valid_from",
					"valid_to",
					"lft",
					"rgt",
				],
				as_dict=True,
			)
			if not unit:
				raise_ione_error("RESOURCE_NOT_FOUND")
			version = frappe.db.get_value(
				"HRP Organization Version",
				unit.organization_version,
				["docstatus", "status", "effective_from"],
				as_dict=True,
			)
			if unit.company != self.company or unit.hospital != self.hospital:
				raise_ione_error("CONFLICT")
			if (
				not bool(unit.enabled)
				or not version
				or int(version.docstatus) != 1
				or version.status != "Published"
			):
				raise_ione_error("INVALID_STATE_TRANSITION")
			if str(version.effective_from) > str(self.valid_from):
				raise_ione_error("CONFLICT")
			if unit.valid_from and str(unit.valid_from) > str(self.valid_from):
				raise_ione_error("CONFLICT")
			if unit.valid_to and (not self.valid_to or str(unit.valid_to) < str(self.valid_to)):
				raise_ione_error("CONFLICT")
			if bool(self.include_descendants) and (unit.lft is None or unit.rgt is None):
				raise_ione_error("CONFIGURATION_INVALID")
		for step in self.steps:
			if step.approver_type == "Role" and not frappe.db.exists("Role", step.approver_role):
				raise_ione_error("RESOURCE_NOT_FOUND")
			if step.approver_type == "User":
				user = frappe.db.get_value(
					"User",
					step.approver_user,
					["enabled", "user_type"],
					as_dict=True,
				)
				if not user:
					raise_ione_error("RESOURCE_NOT_FOUND")
				if not bool(user.enabled) or user.user_type != "System User":
					raise_ione_error("INVALID_STATE_TRANSITION")

	@staticmethod
	def lock_revision(expected_revision: object, name: str) -> int:
		try:
			expected = normalize_positive_integer(expected_revision, label="expected_revision")
		except ValueError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		rows = frappe.db.sql(
			"""
			SELECT revision
			FROM `tabHRP Approval Matrix`
			WHERE name = %s
			FOR UPDATE
			""",
			name,
			as_dict=True,
		)
		if not rows:
			raise_ione_error("RESOURCE_NOT_FOUND")
		if int(rows[0].revision or 0) != expected:
			raise_ione_error("CONFLICT")
		return expected

	def as_public_dict(self) -> dict[str, object]:
		return {"name": self.name, **self.as_definition(revision=self.revision).as_public_dict()}


__all__ = ["HRPApprovalMatrix"]
