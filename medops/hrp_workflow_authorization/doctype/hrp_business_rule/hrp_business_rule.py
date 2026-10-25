from __future__ import annotations

import frappe
from frappe.model.document import Document

from medops.common.business_rules import POLICY_FIELDS, BusinessRuleError, policy_digest, rule_payload
from medops.common.domain_service import canonical_json
from medops.hrp_workflow_authorization.services.rule_contracts import build_rule_contracts, load_contracts
from medops.hrp_workflow_authorization.services.segregation import get_governed_document_context
from medops.services.errors import raise_ione_error


class HRPBusinessRule(Document):
	def validate(self) -> None:
		if not self.flags.business_rule_service_write:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		try:
			payload = rule_payload({key: self.get(key) for key in POLICY_FIELDS})
			contracts = build_rule_contracts(payload)
		except ValueError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		for key, value in payload.items():
			self.set(key, value)
		self.validate_scope()
		before = self.get_doc_before_save()
		if before and any(
			before.get(key) != self.get(key)
			for key in ("code", "target_doctype", "target_action", "company", "hospital", "organization_unit")
		):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		self.field_contract_json = canonical_json(contracts)
		self.policy_digest = policy_digest(payload, contracts)
		self.revision = (
			1 if not before else int(before.revision) + int(before.policy_digest != self.policy_digest)
		)

	def validate_scope(self) -> None:
		hospital = frappe.db.get_value(
			"HRP Hospital", self.hospital, ["company", "enabled", "valid_from", "valid_to"], as_dict=True
		)
		if not hospital or hospital.company != self.company or not hospital.enabled:
			raise_ione_error("CONFIGURATION_INVALID")
		if (hospital.valid_from and str(hospital.valid_from) > str(self.valid_from)) or (
			hospital.valid_to and (not self.valid_to or str(self.valid_to) > str(hospital.valid_to))
		):
			raise_ione_error("CONFIGURATION_INVALID")
		if self.organization_unit:
			get_governed_document_context(self)

	def runtime_definition(self) -> tuple[dict, dict]:
		try:
			payload = rule_payload({key: self.get(key) for key in POLICY_FIELDS})
			contracts = build_rule_contracts(payload)
			if (
				contracts != load_contracts(self.field_contract_json)
				or policy_digest(payload, contracts) != self.policy_digest
			):
				raise BusinessRuleError("rule metadata or digest changed")
		except (ValueError, KeyError, TypeError) as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		self.validate_scope()
		return payload, contracts

	def on_trash(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")
