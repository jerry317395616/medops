from __future__ import annotations

from typing import cast

import frappe
from frappe.utils import today

from medops.common.domain_service import DomainServiceDefinition, DomainServiceExecution
from medops.common.organization_mapping import (
	OrganizationMappingResolve,
	OrganizationMappingUpsert,
)
from medops.hrp_organization.doctype.hrp_organization_mapping.hrp_organization_mapping import (
	HRPOrganizationMapping,
)
from medops.hrp_organization.services.organization import ORGANIZATION_ADMIN_ROLES
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.audit_context import emit_audit_event
from medops.services.data_access import public_fields_readable, require_scope, require_scoped_read
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error

ORGANIZATION_MAPPING_READ_ROLES = frozenset(
	{
		*ORGANIZATION_ADMIN_ROLES,
		"HRP Integration User",
	}
)
STANDARD_TARGETS = {
	"department": "Department",
	"cost_center": "Cost Center",
}


def _execution_payload(execution: DomainServiceExecution) -> dict[str, object]:
	return {
		**execution.result,
		"correlation_id": execution.correlation_id,
		"request_id": execution.request_id,
		"idempotency_replayed": execution.idempotency_replayed,
	}


def _mapping_doc(name: str) -> HRPOrganizationMapping:
	if not frappe.db.exists("HRP Organization Mapping", name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(
		HRPOrganizationMapping,
		frappe.get_doc("HRP Organization Mapping", name),
	)


def _lock_unit(command: OrganizationMappingUpsert) -> frappe._dict:
	rows = frappe.db.sql(
		"""
		SELECT
			unit.name,
			unit.organization_version,
			unit.company,
			unit.hospital,
			unit.code,
			unit.unit_type,
			unit.enabled,
			unit.lft,
			unit.rgt,
			version.docstatus,
			version.status
		FROM `tabHRP Organization Unit` AS unit
		INNER JOIN `tabHRP Organization Version` AS version
			ON version.name = unit.organization_version
		WHERE unit.name = %s
			AND version.name = %s
		FOR UPDATE
		""",
		(command.organization_unit, command.organization_version),
		as_dict=True,
	)
	if not rows:
		raise_ione_error("RESOURCE_NOT_FOUND")
	unit = rows[0]
	if int(unit.docstatus) != 1 or unit.status != "Published":
		raise_ione_error("INVALID_STATE_TRANSITION")
	if command.enabled and not bool(unit.enabled):
		raise_ione_error("INVALID_STATE_TRANSITION")
	return unit


def _lock_existing_mapping(organization_unit: str) -> frappe._dict | None:
	rows = frappe.db.sql(
		"""
		SELECT
			name,
			department,
			cost_center,
			enabled,
			revision,
			remarks
		FROM `tabHRP Organization Mapping`
		WHERE organization_unit = %s
		FOR UPDATE
		""",
		organization_unit,
		as_dict=True,
	)
	return rows[0] if rows else None


def _lock_standard_target(
	*,
	fieldname: str,
	name: str,
	company: str,
	require_enabled: bool,
) -> frappe._dict:
	doctype = STANDARD_TARGETS[fieldname]
	rows = frappe.db.sql(
		f"""
		SELECT name, company, disabled, is_group, lft, rgt
		FROM `tab{doctype}`
		WHERE name = %s
		FOR UPDATE
		""",  # nosec B608 -- table names come only from the static STANDARD_TARGETS registry.
		name,
		as_dict=True,
	)
	if not rows:
		raise_ione_error("RESOURCE_NOT_FOUND")
	target = rows[0]
	if target.company != company:
		raise_ione_error("CONFLICT")
	if require_enabled and bool(target.disabled):
		raise_ione_error("INVALID_STATE_TRANSITION")
	try:
		if int(target.lft or 0) < 1 or int(target.rgt or 0) <= int(target.lft or 0):
			raise_ione_error("CONFIGURATION_INVALID")
	except (TypeError, ValueError) as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)
	return target


def _lock_related_targets(
	*,
	organization_version: str,
	organization_unit: str,
	fieldname: str,
) -> list[frappe._dict]:
	return frappe.db.sql(
		f"""
		SELECT unit.lft, unit.rgt, mapping.`{fieldname}` AS target
		FROM `tabHRP Organization Mapping` AS mapping
		INNER JOIN `tabHRP Organization Unit` AS unit
			ON unit.name = mapping.organization_unit
		WHERE mapping.organization_version = %s
			AND mapping.organization_unit != %s
			AND mapping.enabled = 1
			AND mapping.`{fieldname}` IS NOT NULL
			AND mapping.`{fieldname}` != ''
		FOR UPDATE
		""",  # nosec B608 -- field names come only from the static STANDARD_TARGETS registry.
		(organization_version, organization_unit),
		as_dict=True,
	)


def _lock_target_ranges(doctype: str, names: set[str]) -> dict[str, frappe._dict]:
	if not names:
		return {}
	placeholders = ", ".join(["%s"] * len(names))
	rows = frappe.db.sql(
		f"""
		SELECT name, lft, rgt
		FROM `tab{doctype}`
		WHERE name IN ({placeholders})
		FOR UPDATE
		""",  # nosec B608 -- table name is static and values remain bound parameters.
		tuple(sorted(names)),
		as_dict=True,
	)
	if len(rows) != len(names):
		raise_ione_error("CONFIGURATION_INVALID")
	return {row.name: row for row in rows}


def _validate_tree_alignment(
	*,
	unit: frappe._dict,
	fieldname: str,
	target: frappe._dict,
) -> None:
	related = _lock_related_targets(
		organization_version=unit.organization_version,
		organization_unit=unit.name,
		fieldname=fieldname,
	)
	if not related:
		return
	doctype = STANDARD_TARGETS[fieldname]
	ranges = _lock_target_ranges(
		doctype,
		{str(row.target) for row in related},
	)
	unit_lft = int(unit.lft)
	unit_rgt = int(unit.rgt)
	target_lft = int(target.lft)
	target_rgt = int(target.rgt)
	for row in related:
		other = ranges[str(row.target)]
		other_lft = int(other.lft)
		other_rgt = int(other.rgt)
		is_organization_ancestor = int(row.lft) < unit_lft and int(row.rgt) > unit_rgt
		is_organization_descendant = int(row.lft) > unit_lft and int(row.rgt) < unit_rgt
		if is_organization_ancestor and not (other_lft < target_lft and other_rgt > target_rgt):
			raise_ione_error("CONFLICT")
		if is_organization_descendant and not (target_lft < other_lft and target_rgt > other_rgt):
			raise_ione_error("CONFLICT")


def _validate_target_uniqueness(
	*,
	command: OrganizationMappingUpsert,
	fieldname: str,
	target_name: str,
) -> None:
	rows = frappe.db.sql(
		f"""
		SELECT name
		FROM `tabHRP Organization Mapping`
		WHERE organization_version = %s
			AND `{fieldname}` = %s
			AND organization_unit != %s
		FOR UPDATE
		""",  # nosec B608 -- field names come only from the static STANDARD_TARGETS registry.
		(
			command.organization_version,
			target_name,
			command.organization_unit,
		),
		as_dict=True,
	)
	if rows:
		raise_ione_error("CONFLICT")


class UpsertOrganizationMappingService(DomainService[OrganizationMappingUpsert]):
	definition = DomainServiceDefinition(
		name="hrp_organization.mapping.upsert",
		version=1,
		kind="command",
		required_roles=ORGANIZATION_ADMIN_ROLES,
	)

	def authorize_result(self, result: dict[str, object]) -> None:
		doc = _mapping_doc(str(result["name"]))
		require_scoped_read(doc)
		if not public_fields_readable(doc):
			raise_ione_error("PERMISSION_DENIED")

	def request_payload(self, command: OrganizationMappingUpsert) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: OrganizationMappingUpsert) -> None:
		unit = frappe.get_doc("HRP Organization Unit", command.organization_unit)
		require_scoped_read(unit)
		if command.expected_revision:
			name = frappe.db.get_value("HRP Organization Mapping", {"organization_unit": unit.name}, "name")
			if name:
				existing = _mapping_doc(name)
				require_scoped_read(existing)
				if not public_fields_readable(existing):
					raise_ione_error("PERMISSION_DENIED")
		require_scope(
			"HRP Organization Mapping",
			company=unit.company,
			hospital=unit.hospital,
			organization_unit=unit.name,
			action="write" if command.expected_revision else "create",
		)
		for field, doctype in STANDARD_TARGETS.items():
			name = getattr(command, field)
			if name:
				target = frappe.get_doc(doctype, name)
				if not frappe.has_permission(doctype, ptype="read", doc=target) or not can_read_named_fields(
					target, {"company", "disabled"}
				):
					raise_ione_error("PERMISSION_DENIED")

	def perform(self, command: OrganizationMappingUpsert) -> dict[str, object]:
		unit = _lock_unit(command)
		existing = _lock_existing_mapping(command.organization_unit)
		if (existing is None) != (command.expected_revision == 0):
			raise_ione_error("CONFLICT")
		if existing is not None and int(existing.revision) != command.expected_revision:
			raise_ione_error("CONFLICT")

		targets = {
			fieldname: target_name
			for fieldname, target_name in (
				("department", command.department),
				("cost_center", command.cost_center),
			)
			if target_name is not None
		}
		for fieldname, target_name in targets.items():
			target = _lock_standard_target(
				fieldname=fieldname,
				name=target_name,
				company=unit.company,
				require_enabled=command.enabled,
			)
			_validate_target_uniqueness(
				command=command,
				fieldname=fieldname,
				target_name=target_name,
			)
			if command.enabled:
				_validate_tree_alignment(
					unit=unit,
					fieldname=fieldname,
					target=target,
				)

		if existing is None:
			doc = cast(
				HRPOrganizationMapping,
				frappe.get_doc(
					{
						"doctype": "HRP Organization Mapping",
						"organization_version": unit.organization_version,
						"organization_unit": unit.name,
						"company": unit.company,
						"hospital": unit.hospital,
						"unit_code": unit.code,
						"unit_type": unit.unit_type,
						"department": command.department,
						"cost_center": command.cost_center,
						"enabled": int(command.enabled),
						"revision": 1,
						"remarks": command.remarks,
					}
				),
			)
			doc.flags.organization_mapping_service_write = True
			doc.insert(ignore_permissions=True)
			emit_audit_event(
				"organization_mapping_created",
				logger_name="medops.organization_mapping",
				revision=1,
				enabled=command.enabled,
				target_count=len(targets),
			)
			return {
				**doc.as_public_dict(),
				"changed": True,
				"changed_fields": ["created"],
			}

		doc = _mapping_doc(existing.name)
		changed_fields = [
			fieldname
			for fieldname, current, requested in (
				("department", doc.department or None, command.department),
				("cost_center", doc.cost_center or None, command.cost_center),
				("enabled", bool(doc.enabled), command.enabled),
				("remarks", doc.remarks or None, command.remarks),
			)
			if current != requested
		]
		if not changed_fields:
			return {
				**doc.as_public_dict(),
				"changed": False,
				"changed_fields": [],
			}
		doc.department = command.department
		doc.cost_center = command.cost_center
		doc.enabled = int(command.enabled)
		doc.remarks = command.remarks
		doc.flags.organization_mapping_service_write = True
		doc.flags.locked_revision = int(existing.revision)
		doc.save(ignore_permissions=True)
		emit_audit_event(
			"organization_mapping_changed",
			logger_name="medops.organization_mapping",
			before_revision=int(existing.revision),
			after_revision=int(doc.revision),
			enabled=command.enabled,
			target_count=len(targets),
			changed_field_count=len(changed_fields),
			changed_fields=",".join(changed_fields),
		)
		return {
			**doc.as_public_dict(),
			"changed": True,
			"changed_fields": changed_fields,
		}


def upsert_organization_mapping(
	command: OrganizationMappingUpsert,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		UpsertOrganizationMappingService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def _resolve_unit(query: OrganizationMappingResolve) -> str:
	if query.organization_unit is not None:
		return query.organization_unit
	effective_on = query.effective_on or today()
	version_rows = frappe.db.sql(
		"""
		SELECT name
		FROM `tabHRP Organization Version`
		WHERE hospital = %s
			AND docstatus = 1
			AND status = 'Published'
			AND effective_from <= %s
		ORDER BY effective_from DESC, version_number DESC
		LIMIT 1
		""",
		(query.hospital, effective_on),
		as_dict=True,
	)
	if not version_rows:
		raise_ione_error("RESOURCE_NOT_FOUND")
	unit = frappe.db.get_value(
		"HRP Organization Unit",
		{
			"organization_version": version_rows[0].name,
			"code": query.unit_code,
		},
		"name",
	)
	if not unit:
		raise_ione_error("RESOURCE_NOT_FOUND")
	return str(unit)


class ResolveOrganizationMappingService(DomainService[OrganizationMappingResolve]):
	definition = DomainServiceDefinition(
		name="hrp_organization.mapping.resolve",
		version=1,
		kind="query",
		required_roles=ORGANIZATION_MAPPING_READ_ROLES,
	)

	def request_payload(self, command: OrganizationMappingResolve) -> dict[str, object]:
		return command.as_request_payload()

	def perform(self, command: OrganizationMappingResolve) -> dict[str, object]:
		organization_unit = _resolve_unit(command)
		doc = _mapping_doc(organization_unit)
		require_scoped_read(doc)
		if not public_fields_readable(doc):
			raise_ione_error("PERMISSION_DENIED")
		if not bool(doc.enabled):
			raise_ione_error("INVALID_STATE_TRANSITION")
		target_count = 0
		for fieldname in STANDARD_TARGETS:
			target_name = doc.get(fieldname)
			if not target_name:
				continue
			target = frappe.get_doc(STANDARD_TARGETS[fieldname], str(target_name))
			if not frappe.has_permission(
				target.doctype, ptype="read", doc=target
			) or not can_read_named_fields(target, {"company", "disabled"}):
				raise_ione_error("PERMISSION_DENIED")
			target_count += 1
			_lock_standard_target(
				fieldname=fieldname,
				name=str(target_name),
				company=doc.company,
				require_enabled=True,
			)
		emit_audit_event(
			"organization_mapping_resolved",
			logger_name="medops.organization_mapping",
			revision=int(doc.revision),
			target_count=target_count,
		)
		return doc.as_public_dict()


def resolve_organization_mapping(
	command: OrganizationMappingResolve,
	*,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		ResolveOrganizationMappingService().execute(
			command,
			correlation_id=correlation_id,
		)
	)


__all__ = [
	"ORGANIZATION_MAPPING_READ_ROLES",
	"ResolveOrganizationMappingService",
	"UpsertOrganizationMappingService",
	"resolve_organization_mapping",
	"upsert_organization_mapping",
]
