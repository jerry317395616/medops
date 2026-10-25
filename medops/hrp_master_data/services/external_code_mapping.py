from __future__ import annotations

from typing import cast

import frappe

from medops.common.domain_service import (
	DomainServiceDefinition,
	DomainServiceExecution,
)
from medops.common.external_code_mapping import (
	ExternalCodeMappingResolve,
	ExternalCodeMappingUpsert,
	InternalCodeMappingResolve,
)
from medops.common.master_data import MASTER_DATA_TARGET_POLICIES, MasterDataTargetPolicy
from medops.hrp_master_data.doctype.hrp_external_code_mapping.hrp_external_code_mapping import (
	HRPExternalCodeMapping,
)
from medops.hrp_master_data.doctype.hrp_master_data_domain.hrp_master_data_domain import (
	HRPMasterDataDomain,
)
from medops.services.audit_context import emit_audit_event
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error

EXTERNAL_CODE_MAPPING_WRITE_ROLES = frozenset({"System Manager", "HRP System Manager", "HRP Data Steward"})
EXTERNAL_CODE_MAPPING_READ_ROLES = frozenset({*EXTERNAL_CODE_MAPPING_WRITE_ROLES, "HRP Integration User"})


def _execution_payload(execution: DomainServiceExecution) -> dict[str, object]:
	return {
		**execution.result,
		"correlation_id": execution.correlation_id,
		"request_id": execution.request_id,
		"idempotency_replayed": execution.idempotency_replayed,
	}


def _mapping_doc(name: str) -> HRPExternalCodeMapping:
	if not frappe.db.exists("HRP External Code Mapping", name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(HRPExternalCodeMapping, frappe.get_doc("HRP External Code Mapping", name))


def _operational_domain(
	name: str,
	*,
	lock: bool,
) -> tuple[HRPMasterDataDomain, MasterDataTargetPolicy]:
	rows = frappe.db.sql(
		"""
		SELECT name
		FROM `tabHRP Master Data Domain`
		WHERE name = %s
		"""
		+ (" FOR UPDATE" if lock else ""),
		name,
		as_dict=True,
	)
	if not rows:
		raise_ione_error("RESOURCE_NOT_FOUND")
	domain = cast(HRPMasterDataDomain, frappe.get_doc("HRP Master Data Domain", name))
	policy = MASTER_DATA_TARGET_POLICIES.get(domain.target_doctype)
	if not bool(domain.enabled):
		raise_ione_error("INVALID_STATE_TRANSITION")
	if (
		policy is None
		or int(domain.policy_version or 0) != policy.version
		or domain.policy_digest != policy.digest
	):
		raise_ione_error("CONFIGURATION_INVALID")
	return domain, policy


def _assert_hospital_scope(
	*,
	company: str,
	hospital: str,
	organization_unit: str | None,
	effective_on: str,
) -> None:
	hospital_row = frappe.db.get_value(
		"HRP Hospital",
		hospital,
		["company", "enabled", "valid_from", "valid_to"],
		as_dict=True,
	)
	if not hospital_row:
		raise_ione_error("RESOURCE_NOT_FOUND")
	if hospital_row.company != company:
		raise_ione_error("CONFLICT")
	if not bool(hospital_row.enabled):
		raise_ione_error("INVALID_STATE_TRANSITION")
	if hospital_row.valid_from and str(hospital_row.valid_from) > effective_on:
		raise_ione_error("CONFLICT")
	if hospital_row.valid_to and str(hospital_row.valid_to) < effective_on:
		raise_ione_error("CONFLICT")
	if organization_unit is None:
		return
	rows = frappe.db.sql(
		"""
		SELECT
			unit.company,
			unit.hospital,
			unit.enabled,
			unit.valid_from,
			unit.valid_to,
			version.docstatus,
			version.status,
			version.effective_from
		FROM `tabHRP Organization Unit` AS unit
		INNER JOIN `tabHRP Organization Version` AS version
			ON version.name = unit.organization_version
		WHERE unit.name = %s
		""",
		organization_unit,
		as_dict=True,
	)
	if not rows:
		raise_ione_error("RESOURCE_NOT_FOUND")
	row = rows[0]
	if row.company != company or row.hospital != hospital:
		raise_ione_error("CONFLICT")
	if not bool(row.enabled) or int(row.docstatus) != 1 or row.status != "Published":
		raise_ione_error("INVALID_STATE_TRANSITION")
	if str(row.effective_from) > effective_on:
		raise_ione_error("CONFLICT")
	if row.valid_from and str(row.valid_from) > effective_on:
		raise_ione_error("CONFLICT")
	if row.valid_to and str(row.valid_to) < effective_on:
		raise_ione_error("CONFLICT")


def assert_master_data_hospital_scope(
	*,
	company: str,
	hospital: str,
	organization_unit: str | None,
	effective_on: str,
) -> None:
	"""Validate the shared master-data organization boundary."""
	_assert_hospital_scope(
		company=company,
		hospital=hospital,
		organization_unit=organization_unit,
		effective_on=effective_on,
	)


def get_operational_master_data_domain(
	domain_code: str,
	*,
	lock: bool,
) -> tuple[HRPMasterDataDomain, MasterDataTargetPolicy]:
	"""Return an enabled master-data domain whose code policy still matches."""
	return _operational_domain(domain_code, lock=lock)


def _assert_internal_target(
	policy: MasterDataTargetPolicy,
	*,
	internal_name: str,
	company: str,
	lock: bool,
) -> None:
	fields = ["name", "disabled"]
	if policy.company_field:
		fields.append(policy.company_field)
	columns = ", ".join(f"`{field}`" for field in fields)
	rows = frappe.db.sql(
		f"""
		SELECT {columns}
		FROM `tab{policy.target_doctype}`
		WHERE name = %s
		"""
		+ (" FOR UPDATE" if lock else ""),
		internal_name,
		as_dict=True,
	)
	if not rows:
		raise_ione_error("RESOURCE_NOT_FOUND")
	row = rows[0]
	if bool(row.disabled):
		raise_ione_error("INVALID_STATE_TRANSITION")
	if policy.company_field and row.get(policy.company_field) != company:
		raise_ione_error("CONFLICT")


def _identity(command: ExternalCodeMappingUpsert) -> tuple[str, ...]:
	return (
		command.master_data_domain,
		command.company,
		command.hospital,
		command.scope_key,
		command.external_system,
		command.external_code,
	)


def _stored_identity(mapping: HRPExternalCodeMapping) -> tuple[str, ...]:
	return (
		mapping.master_data_domain,
		mapping.company,
		mapping.hospital,
		mapping.scope_key,
		mapping.external_system,
		mapping.external_code,
	)


def _conflicting_mapping(
	command: ExternalCodeMappingUpsert,
	*,
	by_internal_target: bool,
) -> str | None:
	column = "target_key" if by_internal_target else "source_key"
	value = command.target_key if by_internal_target else command.source_key
	rows = frappe.db.sql(
		f"""
		SELECT name
		FROM `tabHRP External Code Mapping`
		WHERE `{column}` = %s
			AND name != %s
		LIMIT 1
		FOR UPDATE
		""",
		(
			value,
			command.mapping_name or "",
		),
		as_dict=True,
	)
	return str(rows[0].name) if rows else None


def _mapping_digests(mapping: HRPExternalCodeMapping) -> tuple[str, str]:
	return str(mapping.source_key), str(mapping.target_key)


class UpsertExternalCodeMappingService(DomainService[ExternalCodeMappingUpsert]):
	definition = DomainServiceDefinition(
		name="hrp_master_data.external_code_mapping.upsert",
		version=1,
		kind="command",
		required_roles=EXTERNAL_CODE_MAPPING_WRITE_ROLES,
	)

	def request_payload(self, command: ExternalCodeMappingUpsert) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: ExternalCodeMappingUpsert) -> None:
		if not frappe.db.exists("Company", command.company):
			raise_ione_error("RESOURCE_NOT_FOUND")
		if command.mapping_name and not frappe.db.exists(
			"HRP External Code Mapping",
			command.mapping_name,
		):
			raise_ione_error("RESOURCE_NOT_FOUND")

	def perform(self, command: ExternalCodeMappingUpsert) -> dict[str, object]:
		domain, policy = _operational_domain(command.master_data_domain, lock=True)
		_assert_hospital_scope(
			company=command.company,
			hospital=command.hospital,
			organization_unit=command.organization_unit,
			effective_on=command.valid_from,
		)
		_assert_internal_target(
			policy,
			internal_name=command.internal_name,
			company=command.company,
			lock=True,
		)
		if _conflicting_mapping(command, by_internal_target=False):
			raise_ione_error("CONFLICT")
		if _conflicting_mapping(command, by_internal_target=True):
			raise_ione_error("CONFLICT")

		if command.mapping_name is None:
			doc = cast(
				HRPExternalCodeMapping,
				frappe.get_doc(
					{
						"doctype": "HRP External Code Mapping",
						"master_data_domain": domain.name,
						"target_doctype": domain.target_doctype,
						"internal_name": command.internal_name,
						"company": command.company,
						"hospital": command.hospital,
						"organization_unit": command.organization_unit,
						"scope_key": command.scope_key,
						"source_key": command.source_key,
						"target_key": command.target_key,
						"external_system": command.external_system,
						"external_code": command.external_code,
						"external_label": command.external_label,
						"enabled": int(command.enabled),
						"valid_from": command.valid_from,
						"valid_to": command.valid_to,
						"revision": 1,
						"remarks": command.remarks,
					}
				),
			)
			doc.flags.external_code_mapping_service_write = True
			try:
				doc.insert(ignore_permissions=True)
			except frappe.DuplicateEntryError as exc:
				raise_ione_error("CONFLICT", cause=exc)
			source_digest, target_digest = _mapping_digests(doc)
			emit_audit_event(
				"external_code_mapping_created",
				logger_name="medops.external_code_mapping",
				revision=1,
				enabled=command.enabled,
				source_digest=source_digest,
				target_digest=target_digest,
			)
			return {**doc.as_public_dict(), "changed": True, "changed_fields": ["created"]}

		current_revision = HRPExternalCodeMapping.lock_revision(
			command.expected_revision,
			command.mapping_name,
		)
		doc = _mapping_doc(command.mapping_name)
		if _stored_identity(doc) != _identity(command):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if doc.target_doctype != domain.target_doctype:
			raise_ione_error("CONFIGURATION_INVALID")
		changed_fields = [
			fieldname
			for fieldname, current, requested in (
				("internal_name", doc.internal_name, command.internal_name),
				("external_label", doc.external_label or None, command.external_label),
				("enabled", bool(doc.enabled), command.enabled),
				("valid_from", str(doc.valid_from), command.valid_from),
				("valid_to", str(doc.valid_to) if doc.valid_to else None, command.valid_to),
				("remarks", doc.remarks or None, command.remarks),
			)
			if current != requested
		]
		if not changed_fields:
			return {**doc.as_public_dict(), "changed": False, "changed_fields": []}
		doc.internal_name = command.internal_name
		doc.external_label = command.external_label
		doc.enabled = int(command.enabled)
		doc.valid_from = command.valid_from
		doc.valid_to = command.valid_to
		doc.remarks = command.remarks
		doc.flags.external_code_mapping_service_write = True
		doc.flags.locked_revision = current_revision
		try:
			doc.save(ignore_permissions=True)
		except frappe.DuplicateEntryError as exc:
			raise_ione_error("CONFLICT", cause=exc)
		source_digest, target_digest = _mapping_digests(doc)
		emit_audit_event(
			"external_code_mapping_changed",
			logger_name="medops.external_code_mapping",
			before_revision=current_revision,
			after_revision=doc.revision,
			enabled=command.enabled,
			changed_field_count=len(changed_fields),
			changed_fields=",".join(changed_fields),
			source_digest=source_digest,
			target_digest=target_digest,
		)
		return {**doc.as_public_dict(), "changed": True, "changed_fields": changed_fields}


def upsert_external_code_mapping(
	command: ExternalCodeMappingUpsert,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		UpsertExternalCodeMappingService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def _resolved_mapping(
	*,
	key_field: str,
	key_value: str,
	effective_on: str,
) -> HRPExternalCodeMapping:
	if key_field not in {"source_key", "target_key"}:
		raise_ione_error("CONFIGURATION_INVALID")
	rows = frappe.db.sql(
		f"""
		SELECT name
		FROM `tabHRP External Code Mapping`
		WHERE `{key_field}` = %(key_value)s
			AND enabled = 1
			AND valid_from <= %(effective_on)s
			AND (valid_to IS NULL OR valid_to >= %(effective_on)s)
		LIMIT 2
		""",
		{
			"key_value": key_value,
			"effective_on": effective_on,
		},
		as_dict=True,
	)
	if len(rows) != 1:
		raise_ione_error("RESOURCE_NOT_FOUND" if not rows else "CONFIGURATION_INVALID")
	return _mapping_doc(str(rows[0].name))


class ResolveExternalCodeMappingService(DomainService[ExternalCodeMappingResolve]):
	definition = DomainServiceDefinition(
		name="hrp_master_data.external_code_mapping.resolve_external",
		version=1,
		kind="query",
		required_roles=EXTERNAL_CODE_MAPPING_READ_ROLES,
	)

	def request_payload(self, command: ExternalCodeMappingResolve) -> dict[str, object]:
		return command.as_request_payload()

	def perform(self, command: ExternalCodeMappingResolve) -> dict[str, object]:
		domain, policy = _operational_domain(command.master_data_domain, lock=False)
		_assert_hospital_scope(
			company=command.company,
			hospital=command.hospital,
			organization_unit=command.organization_unit,
			effective_on=command.effective_on,
		)
		mapping = _resolved_mapping(
			key_field="source_key",
			key_value=command.source_key,
			effective_on=command.effective_on,
		)
		if mapping.target_doctype != domain.target_doctype:
			raise_ione_error("CONFIGURATION_INVALID")
		_assert_internal_target(
			policy,
			internal_name=mapping.internal_name,
			company=command.company,
			lock=False,
		)
		emit_audit_event(
			"external_code_mapping_resolved",
			logger_name="medops.external_code_mapping",
			revision=mapping.revision,
			direction="inbound",
			source_digest=_mapping_digests(mapping)[0],
		)
		return mapping.as_public_dict()


class ResolveInternalCodeMappingService(DomainService[InternalCodeMappingResolve]):
	definition = DomainServiceDefinition(
		name="hrp_master_data.external_code_mapping.resolve_internal",
		version=1,
		kind="query",
		required_roles=EXTERNAL_CODE_MAPPING_READ_ROLES,
	)

	def request_payload(self, command: InternalCodeMappingResolve) -> dict[str, object]:
		return command.as_request_payload()

	def perform(self, command: InternalCodeMappingResolve) -> dict[str, object]:
		domain, policy = _operational_domain(command.master_data_domain, lock=False)
		_assert_hospital_scope(
			company=command.company,
			hospital=command.hospital,
			organization_unit=command.organization_unit,
			effective_on=command.effective_on,
		)
		_assert_internal_target(
			policy,
			internal_name=command.internal_name,
			company=command.company,
			lock=False,
		)
		mapping = _resolved_mapping(
			key_field="target_key",
			key_value=command.target_key,
			effective_on=command.effective_on,
		)
		if mapping.target_doctype != domain.target_doctype:
			raise_ione_error("CONFIGURATION_INVALID")
		emit_audit_event(
			"external_code_mapping_resolved",
			logger_name="medops.external_code_mapping",
			revision=mapping.revision,
			direction="outbound",
			target_digest=_mapping_digests(mapping)[1],
		)
		return mapping.as_public_dict()


def resolve_external_code_mapping(
	command: ExternalCodeMappingResolve,
	*,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		ResolveExternalCodeMappingService().execute(
			command,
			correlation_id=correlation_id,
		)
	)


def resolve_internal_code_mapping(
	command: InternalCodeMappingResolve,
	*,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		ResolveInternalCodeMappingService().execute(
			command,
			correlation_id=correlation_id,
		)
	)


__all__ = [
	"EXTERNAL_CODE_MAPPING_READ_ROLES",
	"EXTERNAL_CODE_MAPPING_WRITE_ROLES",
	"ResolveExternalCodeMappingService",
	"ResolveInternalCodeMappingService",
	"UpsertExternalCodeMappingService",
	"assert_master_data_hospital_scope",
	"get_operational_master_data_domain",
	"resolve_external_code_mapping",
	"resolve_internal_code_mapping",
	"upsert_external_code_mapping",
]
