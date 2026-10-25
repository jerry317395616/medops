from __future__ import annotations

import json
from typing import TYPE_CHECKING

import frappe
from frappe.model.document import Document

from medops.common.error_catalog import IoneApplicationError
from medops.common.organization import normalize_positive_integer
from medops.common.segregation import (
	SegregationContractError,
	SegregationRuleDefinition,
	build_segregation_rule_definition,
)
from medops.hrp_workflow_authorization.services.access_scope import dimension_fields_for
from medops.services.audit_context import emit_audit_event
from medops.services.errors import raise_ione_error


class HRPSegregationRule(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		actor_fields_json: DF.Code | None
		code: DF.Data
		company: DF.Link
		conflicting_roles_json: DF.Code | None
		display_name: DF.Data
		enabled: DF.Check
		hospital: DF.Link
		include_descendants: DF.Check
		organization_unit: DF.Link | None
		policy_digest: DF.Data
		remarks: DF.SmallText | None
		revision: DF.Int
		target_action: DF.Select
		target_doctype: DF.Link
		valid_from: DF.Date
		valid_to: DF.Date | None
	# end: auto-generated types

	def before_insert(self) -> None:
		self._require_service_write()
		self.revision = 1

	def before_save(self) -> None:
		self._require_service_write()
		if self.is_new():
			self.revision = 1
			return
		before = self.get_doc_before_save()
		if before is None or before.policy_digest == self.policy_digest:
			return
		locked_revision = getattr(self.flags, "locked_revision", None)
		self.revision = int(locked_revision if locked_revision is not None else before.revision) + 1

	def validate(self) -> None:
		definition = self.as_definition(revision=self.revision or 1)
		for fieldname, value in (
			("code", definition.code),
			("display_name", definition.display_name),
			("target_doctype", definition.target_doctype),
			("target_action", definition.target_action),
			("company", definition.company),
			("hospital", definition.hospital),
			("organization_unit", definition.organization_unit),
			("include_descendants", int(definition.include_descendants)),
			(
				"actor_fields_json",
				json.dumps(list(definition.actor_fields), ensure_ascii=False, separators=(",", ":")),
			),
			(
				"conflicting_roles_json",
				json.dumps(list(definition.conflicting_roles), ensure_ascii=False, separators=(",", ":")),
			),
			("enabled", int(definition.enabled)),
			("valid_from", definition.valid_from),
			("valid_to", definition.valid_to),
			("remarks", definition.remarks),
			("policy_digest", definition.policy_digest),
		):
			self.set(fieldname, value)
		self._validate_references(definition)
		before = self.get_doc_before_save()
		if before is not None:
			for fieldname in (
				"code",
				"target_doctype",
				"target_action",
				"company",
				"hospital",
				"organization_unit",
			):
				if before.get(fieldname) != self.get(fieldname):
					raise_ione_error("OPERATION_NOT_ALLOWED")

	def on_update(self) -> None:
		emit_audit_event(
			"segregation_rule_saved",
			logger_name="medops.segregation",
			policy_digest=self.policy_digest,
			revision=int(self.revision),
			enabled=bool(self.enabled),
			actor_field_count=len(self.as_definition(revision=self.revision).actor_fields),
			conflicting_role_count=len(self.as_definition(revision=self.revision).conflicting_roles),
		)

	def on_trash(self) -> None:
		if not getattr(self.flags, "segregation_rule_migration", False):
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def as_definition(self, *, revision: object) -> SegregationRuleDefinition:
		try:
			return build_segregation_rule_definition(
				code=self.code,
				display_name=self.display_name,
				target_doctype=self.target_doctype,
				target_action=self.target_action,
				company=self.company,
				hospital=self.hospital,
				organization_unit=self.organization_unit,
				include_descendants=self.include_descendants,
				actor_fields=self.actor_fields_json,
				conflicting_roles=self.conflicting_roles_json,
				enabled=self.enabled,
				valid_from=self.valid_from,
				valid_to=self.valid_to,
				revision=revision,
				remarks=self.remarks,
			)
		except SegregationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)

	def as_runtime_definition(self) -> SegregationRuleDefinition:
		try:
			definition = self.as_definition(revision=self.revision)
		except IoneApplicationError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		self.assert_runtime_references(definition)
		return definition

	def _validate_references(self, definition: SegregationRuleDefinition) -> None:
		for doctype, name in (
			("Company", definition.company),
			("DocType", definition.target_doctype),
			("HRP Hospital", definition.hospital),
		):
			if not frappe.db.exists(doctype, name):
				raise_ione_error("RESOURCE_NOT_FOUND")
		if set(dimension_fields_for(definition.target_doctype)) != {
			"company",
			"hospital",
			"organization_unit",
		}:
			raise_ione_error("CONFIGURATION_INVALID")
		meta = frappe.get_meta(definition.target_doctype)
		for fieldname in definition.actor_fields:
			if fieldname in {"owner", "modified_by"}:
				continue
			field = meta.get_field(fieldname)
			if not field or field.fieldtype != "Link" or field.options != "User":
				raise_ione_error("CONFIGURATION_INVALID")
		for role in definition.conflicting_roles:
			role_state = frappe.db.get_value("Role", role, ["name", "disabled"], as_dict=True)
			if not role_state:
				raise_ione_error("RESOURCE_NOT_FOUND")
			if bool(role_state.disabled):
				raise_ione_error("INVALID_STATE_TRANSITION")
		hospital = frappe.db.get_value(
			"HRP Hospital",
			definition.hospital,
			["company", "enabled", "valid_from", "valid_to"],
			as_dict=True,
		)
		if not hospital or hospital.company != definition.company:
			raise_ione_error("CONFLICT")
		if not bool(hospital.enabled):
			raise_ione_error("INVALID_STATE_TRANSITION")
		if hospital.valid_from and str(hospital.valid_from) > definition.valid_from:
			raise_ione_error("CONFLICT")
		if hospital.valid_to and (not definition.valid_to or str(hospital.valid_to) < definition.valid_to):
			raise_ione_error("CONFLICT")
		if definition.organization_unit:
			unit = frappe.db.get_value(
				"HRP Organization Unit",
				definition.organization_unit,
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
			if unit.company != definition.company or unit.hospital != definition.hospital:
				raise_ione_error("CONFLICT")
			if (
				not bool(unit.enabled)
				or not version
				or int(version.docstatus) != 1
				or version.status != "Published"
			):
				raise_ione_error("INVALID_STATE_TRANSITION")
			if str(version.effective_from) > definition.valid_from:
				raise_ione_error("CONFLICT")
			if unit.valid_from and str(unit.valid_from) > definition.valid_from:
				raise_ione_error("CONFLICT")
			if unit.valid_to and (not definition.valid_to or str(unit.valid_to) < definition.valid_to):
				raise_ione_error("CONFLICT")
			if definition.include_descendants:
				try:
					left = int(unit.lft)
					right = int(unit.rgt)
				except (TypeError, ValueError) as exc:
					raise_ione_error("CONFIGURATION_INVALID", cause=exc)
				if left < 1 or right <= left:
					raise_ione_error("CONFIGURATION_INVALID")

	def assert_runtime_references(self, definition: SegregationRuleDefinition) -> None:
		"""Fail closed when a persisted rule no longer matches its trusted references."""
		if str(self.policy_digest or "") != definition.policy_digest:
			raise_ione_error("CONFIGURATION_INVALID")
		try:
			self._validate_references(definition)
		except IoneApplicationError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)

	def _require_service_write(self) -> None:
		if not getattr(self.flags, "segregation_rule_service_write", False):
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
			FROM `tabHRP Segregation Rule`
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


__all__ = ["HRPSegregationRule"]
