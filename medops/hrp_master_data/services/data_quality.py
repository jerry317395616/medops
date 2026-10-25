from __future__ import annotations

from hashlib import sha256
from typing import cast

import frappe
from frappe.utils import now_datetime, today

from medops.common.data_quality import (
	DATA_QUALITY_ISSUE_DOCTYPE,
	DATA_QUALITY_RULE_DOCTYPE,
	DataQualityContractError,
	DataQualityEvaluate,
	DataQualityRuleUpsert,
	build_data_quality_evaluate,
	build_data_quality_rule_upsert,
	evaluate_quality_value,
	issue_key_for,
	observed_value_digest,
	validate_rule_for_policy,
)
from medops.common.domain_service import DomainServiceDefinition, DomainServiceExecution
from medops.common.error_catalog import IoneApplicationError
from medops.common.master_data import MasterDataFieldPolicy, MasterDataTargetPolicy
from medops.hrp_master_data.doctype.hrp_data_quality_issue.hrp_data_quality_issue import (
	HRPDataQualityIssue,
)
from medops.hrp_master_data.doctype.hrp_data_quality_rule.hrp_data_quality_rule import (
	HRPDataQualityRule,
)
from medops.hrp_master_data.services.external_code_mapping import (
	assert_master_data_hospital_scope,
	get_operational_master_data_domain,
)
from medops.services.audit_context import emit_audit_event, enqueue_with_audit, service_audit_scope
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_roles

DATA_QUALITY_WRITE_ROLES = frozenset({"System Manager", "HRP System Manager", "HRP Data Steward"})
DATA_QUALITY_READ_ROLES = DATA_QUALITY_WRITE_ROLES
DATA_QUALITY_BATCH_SIZE = 200


def _execution_payload(execution: DomainServiceExecution) -> dict[str, object]:
	return {
		**execution.result,
		"correlation_id": execution.correlation_id,
		"request_id": execution.request_id,
		"idempotency_replayed": execution.idempotency_replayed,
	}


def _rule_doc(name: str) -> HRPDataQualityRule:
	if not frappe.db.exists(DATA_QUALITY_RULE_DOCTYPE, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(HRPDataQualityRule, frappe.get_doc(DATA_QUALITY_RULE_DOCTYPE, name))


def _issue_doc(name: str) -> HRPDataQualityIssue:
	if not frappe.db.exists(DATA_QUALITY_ISSUE_DOCTYPE, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(HRPDataQualityIssue, frappe.get_doc(DATA_QUALITY_ISSUE_DOCTYPE, name))


def _rule_command(rule: HRPDataQualityRule) -> DataQualityRuleUpsert:
	try:
		return build_data_quality_rule_upsert(
			rule_name=rule.name,
			code=rule.code,
			display_name=rule.display_name,
			master_data_domain=rule.master_data_domain,
			company=rule.company,
			hospital=rule.hospital,
			organization_unit=rule.organization_unit,
			target_field=rule.target_field,
			rule_type=rule.rule_type,
			parameters=rule.parameters_json,
			severity=rule.severity,
			enabled=rule.enabled,
			valid_from=rule.valid_from,
			valid_to=rule.valid_to,
			expected_revision=rule.revision,
			remarks=rule.remarks,
		)
	except DataQualityContractError as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)


def _identity(command: DataQualityRuleUpsert) -> tuple[str, ...]:
	return (
		command.code,
		command.master_data_domain,
		command.company,
		command.hospital,
		command.organization_unit or "",
	)


def _stored_identity(rule: HRPDataQualityRule) -> tuple[str, ...]:
	return (
		rule.code,
		rule.master_data_domain,
		rule.company,
		rule.hospital,
		rule.organization_unit or "",
	)


class UpsertDataQualityRuleService(DomainService[DataQualityRuleUpsert]):
	definition = DomainServiceDefinition(
		name="hrp_master_data.data_quality.rule_upsert",
		version=1,
		kind="command",
		required_roles=DATA_QUALITY_WRITE_ROLES,
	)

	def request_payload(self, command: DataQualityRuleUpsert) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: DataQualityRuleUpsert) -> None:
		if not frappe.db.exists("Company", command.company):
			raise_ione_error("RESOURCE_NOT_FOUND")
		if command.rule_name and not frappe.db.exists(DATA_QUALITY_RULE_DOCTYPE, command.rule_name):
			raise_ione_error("RESOURCE_NOT_FOUND")

	def perform(self, command: DataQualityRuleUpsert) -> dict[str, object]:
		domain, policy = get_operational_master_data_domain(command.master_data_domain, lock=True)
		try:
			validate_rule_for_policy(command, policy)
		except DataQualityContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		assert_master_data_hospital_scope(
			company=command.company,
			hospital=command.hospital,
			organization_unit=command.organization_unit,
			effective_on=command.valid_from,
		)

		if command.rule_name is None:
			doc = cast(
				HRPDataQualityRule,
				frappe.get_doc(
					{
						"doctype": DATA_QUALITY_RULE_DOCTYPE,
						"master_data_domain": domain.name,
						"target_doctype": domain.target_doctype,
						"code": command.code,
						"display_name": command.display_name,
						"company": command.company,
						"hospital": command.hospital,
						"organization_unit": command.organization_unit,
						"target_field": command.target_field,
						"rule_type": command.rule_type,
						"parameters_json": command.parameters_json,
						"severity": command.severity,
						"enabled": int(command.enabled),
						"valid_from": command.valid_from,
						"valid_to": command.valid_to,
						"rule_digest": command.rule_digest,
						"revision": 1,
						"remarks": command.remarks,
					}
				),
			)
			doc.flags.data_quality_service_write = True
			try:
				doc.insert(ignore_permissions=True)
			except frappe.DuplicateEntryError as exc:
				raise_ione_error("CONFLICT", cause=exc)
			emit_audit_event(
				"data_quality_rule_created",
				logger_name="medops.data_quality",
				rule_digest=doc.rule_digest,
				revision=1,
				rule_type=doc.rule_type,
				severity=doc.severity,
			)
			return {**doc.as_public_dict(), "changed": True, "changed_fields": ["created"]}

		current_revision = HRPDataQualityRule.lock_revision(
			command.expected_revision,
			command.rule_name,
		)
		doc = _rule_doc(command.rule_name)
		if _stored_identity(doc) != _identity(command):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if doc.target_doctype != domain.target_doctype:
			raise_ione_error("CONFIGURATION_INVALID")
		changed_fields = [
			fieldname
			for fieldname, current, requested in (
				("display_name", doc.display_name, command.display_name),
				("target_field", doc.target_field, command.target_field),
				("rule_type", doc.rule_type, command.rule_type),
				("parameters_json", doc.parameters_json, command.parameters_json),
				("severity", doc.severity, command.severity),
				("enabled", bool(doc.enabled), command.enabled),
				("valid_from", str(doc.valid_from), command.valid_from),
				("valid_to", str(doc.valid_to) if doc.valid_to else None, command.valid_to),
				("remarks", doc.remarks or None, command.remarks),
			)
			if current != requested
		]
		if not changed_fields:
			return {**doc.as_public_dict(), "changed": False, "changed_fields": []}
		doc.display_name = command.display_name
		doc.target_field = command.target_field
		doc.rule_type = command.rule_type
		doc.parameters_json = command.parameters_json
		doc.severity = command.severity
		doc.enabled = int(command.enabled)
		doc.valid_from = command.valid_from
		doc.valid_to = command.valid_to
		doc.rule_digest = command.rule_digest
		doc.remarks = command.remarks
		doc.flags.data_quality_service_write = True
		doc.flags.locked_revision = current_revision
		doc.save(ignore_permissions=True)
		emit_audit_event(
			"data_quality_rule_changed",
			logger_name="medops.data_quality",
			rule_digest=doc.rule_digest,
			before_revision=current_revision,
			after_revision=doc.revision,
			changed_field_count=len(changed_fields),
			changed_fields=",".join(changed_fields),
		)
		return {**doc.as_public_dict(), "changed": True, "changed_fields": changed_fields}


def upsert_data_quality_rule(
	command: DataQualityRuleUpsert,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		UpsertDataQualityRuleService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def _target_value(
	policy: MasterDataTargetPolicy,
	field: MasterDataFieldPolicy,
	*,
	target_name: str,
	company: str,
) -> object:
	columns = {"name", field.field_name}
	if policy.company_field:
		columns.add(policy.company_field)
	# Table and field identifiers come only from the static master-data policy registry.
	column_sql = ", ".join(f"`{column}`" for column in sorted(columns))
	rows = frappe.db.sql(
		f"""
		SELECT {column_sql}
		FROM `tab{policy.target_doctype}`
		WHERE name = %s
		FOR UPDATE
		""",  # nosec B608
		target_name,
		as_dict=True,
	)
	if not rows:
		raise_ione_error("RESOURCE_NOT_FOUND")
	row = rows[0]
	if policy.company_field and row.get(policy.company_field) != company:
		raise_ione_error("CONFLICT")
	return row.get(field.field_name)


def _existing_issue(issue_key: str) -> tuple[HRPDataQualityIssue | None, int | None]:
	rows = frappe.db.sql(
		"""
		SELECT name, revision
		FROM `tabHRP Data Quality Issue`
		WHERE issue_key = %s
		LIMIT 1
		FOR UPDATE
		""",
		issue_key,
		as_dict=True,
	)
	if not rows:
		return None, None
	return _issue_doc(str(rows[0].name)), int(rows[0].revision or 0)


class EvaluateDataQualityService(DomainService[DataQualityEvaluate]):
	definition = DomainServiceDefinition(
		name="hrp_master_data.data_quality.evaluate",
		version=1,
		kind="command",
		required_roles=DATA_QUALITY_WRITE_ROLES,
	)

	def request_payload(self, command: DataQualityEvaluate) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: DataQualityEvaluate) -> None:
		if not frappe.db.exists(DATA_QUALITY_RULE_DOCTYPE, command.rule_name):
			raise_ione_error("RESOURCE_NOT_FOUND")

	def perform(self, command: DataQualityEvaluate) -> dict[str, object]:
		current_revision = HRPDataQualityRule.lock_revision(
			command.expected_rule_revision,
			command.rule_name,
		)
		rule = _rule_doc(command.rule_name)
		if not bool(rule.enabled):
			raise_ione_error("INVALID_STATE_TRANSITION")
		if str(rule.valid_from) > command.effective_on or (
			rule.valid_to and str(rule.valid_to) < command.effective_on
		):
			raise_ione_error("INVALID_STATE_TRANSITION")
		domain, policy = get_operational_master_data_domain(rule.master_data_domain, lock=False)
		if domain.target_doctype != rule.target_doctype:
			raise_ione_error("CONFIGURATION_INVALID")
		assert_master_data_hospital_scope(
			company=rule.company,
			hospital=rule.hospital,
			organization_unit=rule.organization_unit,
			effective_on=command.effective_on,
		)
		rule_command = _rule_command(rule)
		try:
			field = validate_rule_for_policy(rule_command, policy)
		except DataQualityContractError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		if rule.rule_digest != rule_command.rule_digest or int(rule.revision) != current_revision:
			raise_ione_error("CONFIGURATION_INVALID")
		value = _target_value(
			policy,
			field,
			target_name=command.target_name,
			company=rule.company,
		)
		reference_exists = None
		if rule.rule_type == "Reference Exists" and value not in (None, ""):
			if not field.link_doctype:
				raise_ione_error("CONFIGURATION_INVALID")
			reference_exists = bool(frappe.db.exists(field.link_doctype, str(value)))
		outcome = evaluate_quality_value(
			rule_type=rule_command.rule_type,
			parameters_json=rule.parameters_json,
			value=value,
			reference_exists=reference_exists,
		)
		value_digest = observed_value_digest(value)
		key = issue_key_for(
			rule_name=rule.name,
			target_doctype=rule.target_doctype,
			target_name=command.target_name,
		)
		issue, issue_revision = _existing_issue(key)
		now = now_datetime()

		if outcome.passed and issue is None:
			emit_audit_event(
				"data_quality_evaluation_passed",
				logger_name="medops.data_quality",
				rule_digest=rule.rule_digest,
				rule_revision=rule.revision,
				issue_key=key,
			)
			return {
				"schema_version": 1,
				"passed": True,
				"failure_code": None,
				"issue": None,
				"changed": False,
			}

		if issue is not None and outcome.passed and issue.issue_status == "Resolved":
			return {
				"schema_version": 1,
				"passed": True,
				"failure_code": None,
				"issue": issue.as_public_dict(),
				"changed": False,
			}

		if issue is None:
			issue = cast(
				HRPDataQualityIssue,
				frappe.get_doc(
					{
						"doctype": DATA_QUALITY_ISSUE_DOCTYPE,
						"quality_rule": rule.name,
						"master_data_domain": rule.master_data_domain,
						"target_doctype": rule.target_doctype,
						"target_name": command.target_name,
						"company": rule.company,
						"hospital": rule.hospital,
						"organization_unit": rule.organization_unit,
						"issue_key": key,
						"issue_status": "Open",
						"severity": rule.severity,
						"failure_code": outcome.failure_code,
						"failure_message": outcome.failure_message,
						"observed_value_digest": value_digest,
						"rule_revision": rule.revision,
						"rule_digest": rule.rule_digest,
						"first_detected_at": now,
						"last_evaluated_at": now,
						"resolved_at": None,
						"occurrence_count": 1,
						"revision": 1,
					}
				),
			)
			issue.flags.data_quality_service_write = True
			try:
				issue.insert(ignore_permissions=True)
			except frappe.DuplicateEntryError as exc:
				raise_ione_error("CONFLICT", cause=exc)
		else:
			issue.issue_status = "Resolved" if outcome.passed else "Open"
			issue.severity = rule.severity
			issue.failure_code = outcome.failure_code
			issue.failure_message = outcome.failure_message
			issue.observed_value_digest = value_digest
			issue.rule_revision = rule.revision
			issue.rule_digest = rule.rule_digest
			issue.last_evaluated_at = now
			issue.resolved_at = now if outcome.passed else None
			if not outcome.passed:
				issue.occurrence_count = int(issue.occurrence_count) + 1
			issue.flags.data_quality_service_write = True
			issue.flags.locked_revision = issue_revision
			issue.save(ignore_permissions=True)

		emit_audit_event(
			"data_quality_issue_resolved" if outcome.passed else "data_quality_issue_detected",
			logger_name="medops.data_quality",
			rule_digest=rule.rule_digest,
			rule_revision=rule.revision,
			issue_key=key,
			issue_revision=issue.revision,
			failure_code=outcome.failure_code,
		)
		return {
			"schema_version": 1,
			"passed": outcome.passed,
			"failure_code": outcome.failure_code,
			"issue": issue.as_public_dict(),
			"changed": True,
		}


def evaluate_data_quality(
	command: DataQualityEvaluate,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		EvaluateDataQualityService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def get_data_quality_issue(
	*,
	issue_name: object,
	correlation_id: object | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_roles(DATA_QUALITY_READ_ROLES)
		if not isinstance(issue_name, str) or not issue_name.strip():
			raise_ione_error("INVALID_REQUEST")
		issue = _issue_doc(issue_name)
		emit_audit_event(
			"data_quality_issue_read",
			logger_name="medops.data_quality",
			issue_key=issue.issue_key,
			issue_status=issue.issue_status,
			revision=issue.revision,
		)
		return issue.as_public_dict()


def _scheduled_idempotency_key(
	*,
	rule_name: str,
	rule_revision: int,
	target_name: str,
	effective_on: str,
) -> str:
	digest = sha256(f"{rule_name}\0{rule_revision}\0{target_name}\0{effective_on}".encode()).hexdigest()
	return f"DQ-{effective_on}-{digest[:32]}"


def run_data_quality_rule_batch(
	*,
	rule_name: str,
	effective_on: str,
	expected_rule_revision: int,
	after_name: str | None = None,
) -> dict[str, object]:
	with service_audit_scope():
		require_roles(DATA_QUALITY_WRITE_ROLES)
		rule = _rule_doc(rule_name)
		if int(rule.revision) != int(expected_rule_revision):
			raise_ione_error("CONFLICT")
		domain, policy = get_operational_master_data_domain(rule.master_data_domain, lock=False)
		if domain.target_doctype != rule.target_doctype:
			raise_ione_error("CONFIGURATION_INVALID")
		filters: dict[str, object] = {"name": (">", after_name)} if after_name else {}
		if policy.company_field:
			filters[policy.company_field] = rule.company
		rows = frappe.get_all(
			policy.target_doctype,
			filters=filters,
			fields=["name"],
			order_by="name asc",
			limit_page_length=DATA_QUALITY_BATCH_SIZE,
		)
		processed = 0
		failed = 0
		for row in rows:
			target_name = str(row.name)
			try:
				evaluate_data_quality(
					build_data_quality_evaluate(
						rule_name=rule.name,
						target_name=target_name,
						effective_on=effective_on,
						expected_rule_revision=rule.revision,
					),
					idempotency_key=_scheduled_idempotency_key(
						rule_name=rule.name,
						rule_revision=int(rule.revision),
						target_name=target_name,
						effective_on=effective_on,
					),
				)
			except IoneApplicationError:
				failed += 1
			processed += 1
		has_more = len(rows) == DATA_QUALITY_BATCH_SIZE
		if has_more:
			enqueue_with_audit(
				"medops.hrp_master_data.services.data_quality.run_data_quality_rule_batch",
				queue="long",
				enqueue_after_commit=True,
				rule_name=rule.name,
				effective_on=effective_on,
				expected_rule_revision=int(rule.revision),
				after_name=str(rows[-1].name),
			)
		emit_audit_event(
			"data_quality_rule_batch_completed",
			logger_name="medops.data_quality",
			rule_digest=rule.rule_digest,
			rule_revision=rule.revision,
			processed=processed,
			failed=failed,
			has_more=has_more,
		)
		return {"processed": processed, "failed": failed, "has_more": has_more}


def run_data_quality_rules() -> dict[str, object]:
	effective_on = today()
	with service_audit_scope():
		require_roles(DATA_QUALITY_WRITE_ROLES)
		rules = frappe.get_all(
			DATA_QUALITY_RULE_DOCTYPE,
			filters={
				"enabled": 1,
				"valid_from": ("<=", effective_on),
			},
			or_filters=[
				{"valid_to": ("is", "not set")},
				{"valid_to": (">=", effective_on)},
			],
			fields=["name", "revision", "rule_digest"],
			order_by="name asc",
		)
		for rule in rules:
			enqueue_with_audit(
				"medops.hrp_master_data.services.data_quality.run_data_quality_rule_batch",
				queue="long",
				enqueue_after_commit=True,
				rule_name=str(rule.name),
				effective_on=effective_on,
				expected_rule_revision=int(rule.revision),
			)
		emit_audit_event(
			"data_quality_daily_run_scheduled",
			logger_name="medops.data_quality",
			rule_count=len(rules),
		)
		return {"scheduled_rule_count": len(rules), "effective_on": effective_on}


__all__ = [
	"DATA_QUALITY_BATCH_SIZE",
	"DATA_QUALITY_READ_ROLES",
	"DATA_QUALITY_WRITE_ROLES",
	"EvaluateDataQualityService",
	"UpsertDataQualityRuleService",
	"evaluate_data_quality",
	"get_data_quality_issue",
	"run_data_quality_rule_batch",
	"run_data_quality_rules",
	"upsert_data_quality_rule",
]
