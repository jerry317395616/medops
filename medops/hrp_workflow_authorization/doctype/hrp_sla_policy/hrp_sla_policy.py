from __future__ import annotations

import json

import frappe
from frappe.model.document import Document

from medops.common.sla import POLICY_FIELDS, SLAContractError, policy_payload, sla_digest
from medops.hrp_workflow_authorization.services.access_scope import dimension_fields_for
from medops.services.errors import raise_ione_error


class HRPSLAPolicy(Document):
	def validate(self) -> None:
		if not self.flags.sla_service_write:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		try:
			payload = policy_payload({key: self.get(key) for key in POLICY_FIELDS})
		except ValueError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		for key, value in payload.items():
			self.set(key, value)
		before = self.get_doc_before_save()
		if before and any(
			before.get(k) != self.get(k)
			for k in ("code", "target_doctype", "company", "hospital", "organization_unit")
		):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		self.policy_digest = sla_digest(payload)
		self.revision = (
			1 if not before else int(before.revision) + int(before.policy_digest != self.policy_digest)
		)
		self.validate_configuration()

	def validate_configuration(self) -> None:
		if self.target_doctype in {"HRP SLA Policy", "HRP SLA Escalation"}:
			raise_ione_error("CONFIGURATION_INVALID")
		if set(dimension_fields_for(self.target_doctype)) != {"company", "hospital", "organization_unit"}:
			raise_ione_error("CONFIGURATION_INVALID")
		meta = frappe.get_meta(self.target_doctype)
		status = meta.get_field(self.status_field)
		start = meta.get_field(self.start_field)
		response = meta.get_field(self.response_field) if self.response_field else None
		if (
			meta.istable
			or meta.issingle
			or not status
			or status.fieldtype != "Select"
			or not start
			or start.fieldtype != "Datetime"
			or (self.response_field and (not response or response.fieldtype != "Datetime"))
		):
			raise_ione_error("CONFIGURATION_INVALID")
		options = set((status.options or "").splitlines())
		for key in ("start_statuses_json", "pause_statuses_json", "completion_statuses_json"):
			if not set(json.loads(self.get(key))) <= options:
				raise_ione_error("CONFIGURATION_INVALID")
		hospital = frappe.db.get_value("HRP Hospital", self.hospital, ["company", "enabled"], as_dict=True)
		if not hospital or hospital.company != self.company or not hospital.enabled:
			raise_ione_error("CONFIGURATION_INVALID")
		if self.organization_unit:
			from medops.hrp_workflow_authorization.services.segregation import get_governed_document_context

			get_governed_document_context(self)
			unit = frappe.db.get_value(
				"HRP Organization Unit",
				self.organization_unit,
				["company", "hospital", "enabled", "organization_version"],
				as_dict=True,
			)
			if (
				not unit
				or not unit.enabled
				or unit.company != self.company
				or unit.hospital != self.hospital
				or frappe.db.get_value("HRP Organization Version", unit.organization_version, "status")
				!= "Published"
			):
				raise_ione_error("CONFIGURATION_INVALID")
		users = {
			self.responsible_user,
			*(row["assigned_to"] for row in json.loads(self.escalation_steps_json)),
		}
		for user in users:
			state = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
			if not state or not state.enabled or state.user_type != "System User":
				raise_ione_error("CONFIGURATION_INVALID")
		if self.holiday_list:
			holiday = frappe.get_doc("Holiday List", self.holiday_list)
			if (
				len(holiday.holidays) > 3660
				or str(holiday.from_date) > str(self.valid_from)
				or (self.valid_to and str(holiday.to_date) < str(self.valid_to))
			):
				raise_ione_error("CONFIGURATION_INVALID")

	def as_policy(self) -> dict:
		try:
			payload = policy_payload({key: self.get(key) for key in POLICY_FIELDS})
		except SLAContractError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		if sla_digest(payload) != self.policy_digest:
			raise_ione_error("CONFIGURATION_INVALID")
		return payload

	def on_trash(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")
