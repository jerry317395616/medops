from __future__ import annotations

from typing import cast

import frappe
from frappe.utils import now_datetime, today

from medops.common.domain_service import DomainServiceDefinition, DomainServiceExecution
from medops.common.organization import (
	HierarchyReplace,
	HospitalUpsert,
	OrganizationContractError,
	OrganizationNode,
	OrganizationVersionCreate,
	OrganizationVersionPublish,
	hierarchy_digest,
	normalize_hierarchy_nodes,
	normalize_required_date,
)
from medops.hrp_organization.doctype.hrp_hospital.hrp_hospital import HRPHospital
from medops.hrp_organization.doctype.hrp_organization_version.hrp_organization_version import (
	HRPOrganizationVersion,
)
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_roles

ORGANIZATION_ADMIN_ROLES = frozenset({"System Manager", "HRP System Manager", "HRP Data Steward"})
EMPTY_HIERARCHY_DIGEST = hierarchy_digest(())


def _execution_payload(execution: DomainServiceExecution) -> dict[str, object]:
	return {
		**execution.result,
		"correlation_id": execution.correlation_id,
		"request_id": execution.request_id,
		"idempotency_replayed": execution.idempotency_replayed,
	}


def _get_hospital(name: str) -> HRPHospital:
	if not frappe.db.exists("HRP Hospital", name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(HRPHospital, frappe.get_doc("HRP Hospital", name))


def _get_version(name: str) -> HRPOrganizationVersion:
	if not frappe.db.exists("HRP Organization Version", name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(
		HRPOrganizationVersion,
		frappe.get_doc("HRP Organization Version", name),
	)


class UpsertHospitalService(DomainService[HospitalUpsert]):
	definition = DomainServiceDefinition(
		name="hrp_organization.hospital.upsert",
		version=1,
		kind="command",
		required_roles=ORGANIZATION_ADMIN_ROLES,
	)

	def request_payload(self, command: HospitalUpsert) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: HospitalUpsert) -> None:
		if not frappe.db.exists("Company", command.company):
			raise_ione_error("RESOURCE_NOT_FOUND")

	def perform(self, command: HospitalUpsert) -> dict[str, object]:
		exists = bool(frappe.db.exists("HRP Hospital", command.code))
		if exists != (command.expected_revision > 0):
			raise_ione_error("CONFLICT")
		if command.expected_revision == 0:
			doc = cast(
				HRPHospital,
				frappe.get_doc(
					{
						"doctype": "HRP Hospital",
						"code": command.code,
						"company": command.company,
						"display_name": command.display_name,
						"enabled": int(command.enabled),
						"valid_from": command.valid_from,
						"valid_to": command.valid_to,
						"remarks": command.remarks,
						"revision": 1,
						"next_version_number": 1,
					}
				),
			)
			doc.insert(ignore_permissions=True)
			emit_audit_event(
				"hospital_created",
				logger_name="medops.organization",
				revision=1,
			)
			return {**doc.as_public_dict(), "changed": True, "changed_fields": ["created"]}

		current_revision = HRPHospital.lock_revision(command.expected_revision, command.code)
		doc = _get_hospital(command.code)
		changed_fields = [
			fieldname
			for fieldname in (
				"company",
				"display_name",
				"enabled",
				"valid_from",
				"valid_to",
				"remarks",
			)
			if (
				bool(doc.get(fieldname))
				if fieldname == "enabled"
				else (str(doc.get(fieldname)) if doc.get(fieldname) else None)
			)
			!= (command.enabled if fieldname == "enabled" else getattr(command, fieldname))
		]
		if not changed_fields:
			return {**doc.as_public_dict(), "changed": False, "changed_fields": []}

		doc.company = command.company
		doc.display_name = command.display_name
		doc.enabled = int(command.enabled)
		doc.valid_from = command.valid_from
		doc.valid_to = command.valid_to
		doc.remarks = command.remarks
		doc.flags.locked_revision = current_revision
		doc.save(ignore_permissions=True)
		emit_audit_event(
			"hospital_changed",
			logger_name="medops.organization",
			before_revision=current_revision,
			after_revision=doc.revision,
			changed_field_count=len(changed_fields),
			changed_fields=",".join(changed_fields),
		)
		return {
			**doc.as_public_dict(),
			"changed": True,
			"changed_fields": changed_fields,
		}


def upsert_hospital(
	command: HospitalUpsert,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		UpsertHospitalService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


class CreateOrganizationVersionService(DomainService[OrganizationVersionCreate]):
	definition = DomainServiceDefinition(
		name="hrp_organization.version.create",
		version=1,
		kind="command",
		required_roles=ORGANIZATION_ADMIN_ROLES,
	)

	def request_payload(self, command: OrganizationVersionCreate) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: OrganizationVersionCreate) -> None:
		if not frappe.db.exists("HRP Hospital", command.hospital):
			raise_ione_error("RESOURCE_NOT_FOUND")

	def perform(self, command: OrganizationVersionCreate) -> dict[str, object]:
		rows = frappe.db.sql(
			"""
			SELECT company, enabled, next_version_number
			FROM `tabHRP Hospital`
			WHERE name = %s
			FOR UPDATE
			""",
			command.hospital,
			as_dict=True,
		)
		if not rows:
			raise_ione_error("RESOURCE_NOT_FOUND")
		hospital = rows[0]
		if not bool(hospital.enabled):
			raise_ione_error("INVALID_STATE_TRANSITION")
		if frappe.db.exists(
			"HRP Organization Version",
			{
				"hospital": command.hospital,
				"effective_from": command.effective_from,
				"docstatus": ("<", 2),
			},
		):
			raise_ione_error("CONFLICT")
		try:
			version_number = int(hospital.next_version_number or 1)
		except (TypeError, ValueError) as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		if version_number < 1:
			raise_ione_error("CONFIGURATION_INVALID")
		version_code = f"V{version_number:04d}"
		name = f"{command.hospital}-{version_code}"
		if len(name) > 140:
			raise_ione_error("INVALID_REQUEST")
		doc = cast(
			HRPOrganizationVersion,
			frappe.get_doc(
				{
					"doctype": "HRP Organization Version",
					"name": name,
					"hospital": command.hospital,
					"company": hospital.company,
					"version_number": version_number,
					"version_code": version_code,
					"version_label": command.version_label,
					"effective_from": command.effective_from,
					"status": "Draft",
					"node_count": 0,
					"hierarchy_digest": EMPTY_HIERARCHY_DIGEST,
					"revision": 1,
					"remarks": command.remarks,
				}
			),
		)
		doc.flags.organization_service_write = True
		doc.insert(ignore_permissions=True)
		frappe.db.sql(
			"""
			UPDATE `tabHRP Hospital`
			SET next_version_number = %s
			WHERE name = %s
			""",
			(version_number + 1, command.hospital),
		)
		emit_audit_event(
			"organization_version_created",
			logger_name="medops.organization",
			version_number=version_number,
			revision=1,
		)
		return doc.as_public_dict()


def create_organization_version(
	command: OrganizationVersionCreate,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		CreateOrganizationVersionService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def _delete_draft_units(organization_version: str) -> None:
	names = frappe.get_all(
		"HRP Organization Unit",
		filters={"organization_version": organization_version},
		pluck="name",
		order_by="lft desc",
	)
	for name in names:
		doc = frappe.get_doc("HRP Organization Unit", name)
		doc.flags.organization_service_write = True
		doc.delete(ignore_permissions=True)


def _insert_units(
	version: HRPOrganizationVersion,
	nodes: tuple[OrganizationNode, ...],
) -> None:
	names_by_code: dict[str, str] = {}
	for node in nodes:
		parent_name = names_by_code.get(node.parent_code or "")
		doc = frappe.get_doc(
			{
				"doctype": "HRP Organization Unit",
				"organization_version": version.name,
				"company": version.company,
				"hospital": version.hospital,
				"code": node.code,
				"display_name": node.display_name,
				"unit_type": node.unit_type,
				"parent_organization_unit": parent_name,
				"is_group": int(node.is_group),
				"enabled": int(node.enabled),
				"sequence": node.sequence,
				"valid_from": node.valid_from,
				"valid_to": node.valid_to,
				"remarks": node.remarks,
			}
		)
		doc.flags.organization_service_write = True
		doc.insert(ignore_permissions=True)
		names_by_code[node.code] = doc.name


def _assert_nodes_apply_on_effective_date(
	nodes: tuple[OrganizationNode, ...],
	effective_from: str,
) -> None:
	for node in nodes:
		if node.valid_from and node.valid_from > effective_from:
			raise_ione_error("CONFLICT")
		if node.valid_to and node.valid_to < effective_from:
			raise_ione_error("CONFLICT")


class ReplaceOrganizationHierarchyService(DomainService[HierarchyReplace]):
	definition = DomainServiceDefinition(
		name="hrp_organization.hierarchy.replace",
		version=1,
		kind="command",
		required_roles=ORGANIZATION_ADMIN_ROLES,
	)

	def request_payload(self, command: HierarchyReplace) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: HierarchyReplace) -> None:
		if not frappe.db.exists("HRP Organization Version", command.organization_version):
			raise_ione_error("RESOURCE_NOT_FOUND")

	def perform(self, command: HierarchyReplace) -> dict[str, object]:
		current_revision = HRPOrganizationVersion.lock_revision(
			command.expected_revision,
			command.organization_version,
		)
		version = _get_version(command.organization_version)
		if version.docstatus != 0 or version.status != "Draft":
			raise_ione_error("INVALID_STATE_TRANSITION")
		if command.nodes[0].code != version.hospital:
			raise_ione_error("CONFLICT")
		_assert_nodes_apply_on_effective_date(
			command.nodes,
			str(version.effective_from),
		)
		if version.hierarchy_digest == command.digest and int(version.node_count or 0) == len(command.nodes):
			return {
				**version.as_public_dict(),
				"changed": False,
			}

		_delete_draft_units(version.name)
		_insert_units(version, command.nodes)
		version.node_count = len(command.nodes)
		version.hierarchy_digest = command.digest
		version.flags.organization_service_write = True
		version.flags.locked_revision = current_revision
		version.save(ignore_permissions=True)
		emit_audit_event(
			"organization_hierarchy_replaced",
			logger_name="medops.organization",
			before_revision=current_revision,
			after_revision=version.revision,
			node_count=len(command.nodes),
			hierarchy_digest=command.digest,
		)
		return {
			**version.as_public_dict(),
			"changed": True,
		}


def replace_organization_hierarchy(
	command: HierarchyReplace,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		ReplaceOrganizationHierarchyService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def _stored_nodes(organization_version: str) -> tuple[OrganizationNode, ...]:
	rows = frappe.get_all(
		"HRP Organization Unit",
		filters={"organization_version": organization_version},
		fields=[
			"name",
			"code",
			"display_name",
			"unit_type",
			"parent_organization_unit",
			"is_group",
			"enabled",
			"sequence",
			"valid_from",
			"valid_to",
			"remarks",
		],
		order_by="lft asc",
	)
	code_by_name = {row.name: row.code for row in rows}
	payload = [
		{
			"code": row.code,
			"display_name": row.display_name,
			"unit_type": row.unit_type,
			"parent_code": code_by_name.get(row.parent_organization_unit),
			"is_group": int(row.is_group),
			"enabled": int(row.enabled),
			"sequence": int(row.sequence),
			"valid_from": str(row.valid_from) if row.valid_from else None,
			"valid_to": str(row.valid_to) if row.valid_to else None,
			"remarks": row.remarks or None,
		}
		for row in rows
	]
	try:
		return normalize_hierarchy_nodes(payload)
	except OrganizationContractError as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)


class PublishOrganizationVersionService(DomainService[OrganizationVersionPublish]):
	definition = DomainServiceDefinition(
		name="hrp_organization.version.publish",
		version=1,
		kind="command",
		required_roles=ORGANIZATION_ADMIN_ROLES,
	)

	def request_payload(self, command: OrganizationVersionPublish) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: OrganizationVersionPublish) -> None:
		if not frappe.db.exists("HRP Organization Version", command.organization_version):
			raise_ione_error("RESOURCE_NOT_FOUND")

	def perform(self, command: OrganizationVersionPublish) -> dict[str, object]:
		current_revision = HRPOrganizationVersion.lock_revision(
			command.expected_revision,
			command.organization_version,
		)
		version = _get_version(command.organization_version)
		if version.docstatus != 0 or version.status != "Draft":
			raise_ione_error("INVALID_STATE_TRANSITION")
		nodes = _stored_nodes(version.name)
		digest = hierarchy_digest(nodes)
		if not nodes or len(nodes) != int(version.node_count or 0) or digest != version.hierarchy_digest:
			raise_ione_error("CONFIGURATION_INVALID")
		version.status = "Published"
		version.published_at = now_datetime()
		version.flags.organization_service_write = True
		version.flags.locked_revision = current_revision
		version.flags.ignore_permissions = True
		version.submit()
		emit_audit_event(
			"organization_version_published",
			logger_name="medops.organization",
			version_number=version.version_number,
			revision=version.revision,
			node_count=version.node_count,
			hierarchy_digest=version.hierarchy_digest,
		)
		return version.as_public_dict()


def publish_organization_version(
	command: OrganizationVersionPublish,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		PublishOrganizationVersionService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def _resolve_version(
	*,
	organization_version: str | None,
	hospital: str | None,
	effective_on: str | None,
) -> HRPOrganizationVersion:
	if organization_version:
		return _get_version(organization_version)
	if not hospital:
		raise_ione_error("INVALID_REQUEST")
	try:
		resolved_date = normalize_required_date(
			effective_on or today(),
			label="effective_on",
		)
	except OrganizationContractError as exc:
		raise_ione_error("INVALID_REQUEST", cause=exc)
	name = frappe.db.get_value(
		"HRP Organization Version",
		{
			"hospital": hospital,
			"docstatus": 1,
			"effective_from": ("<=", resolved_date),
		},
		"name",
		order_by="effective_from desc, version_number desc",
	)
	if not name:
		raise_ione_error("RESOURCE_NOT_FOUND")
	return _get_version(name)


def _public_nodes(organization_version: str) -> list[dict[str, object]]:
	rows = frappe.get_all(
		"HRP Organization Unit",
		filters={"organization_version": organization_version},
		fields=[
			"name",
			"code",
			"display_name",
			"unit_type",
			"parent_organization_unit",
			"is_group",
			"enabled",
			"sequence",
			"valid_from",
			"valid_to",
			"lft",
			"rgt",
			"remarks",
		],
		order_by="lft asc",
	)
	code_by_name = {row.name: row.code for row in rows}
	stack: list[int] = []
	result: list[dict[str, object]] = []
	for row in rows:
		left = int(row.lft or 0)
		right = int(row.rgt or 0)
		while stack and left > stack[-1]:
			stack.pop()
		result.append(
			{
				"code": row.code,
				"display_name": row.display_name,
				"unit_type": row.unit_type,
				"parent_code": code_by_name.get(row.parent_organization_unit),
				"is_group": bool(row.is_group),
				"enabled": bool(row.enabled),
				"sequence": int(row.sequence),
				"depth": len(stack),
				"valid_from": str(row.valid_from) if row.valid_from else None,
				"valid_to": str(row.valid_to) if row.valid_to else None,
				"remarks": row.remarks or None,
			}
		)
		stack.append(right)
	return result


def get_organization_hierarchy(
	*,
	organization_version: str | None = None,
	hospital: str | None = None,
	effective_on: str | None = None,
) -> dict[str, object]:
	with service_audit_scope():
		require_roles(ORGANIZATION_ADMIN_ROLES)
		version = _resolve_version(
			organization_version=organization_version,
			hospital=hospital,
			effective_on=effective_on,
		)
		nodes = _public_nodes(version.name)
		if len(nodes) != int(version.node_count or 0):
			raise_ione_error("CONFIGURATION_INVALID")
		emit_audit_event(
			"organization_hierarchy_read",
			logger_name="medops.organization",
			version_number=version.version_number,
			revision=version.revision,
			node_count=len(nodes),
		)
		return {
			"schema_version": 1,
			"version": version.as_public_dict(),
			"nodes": nodes,
		}


__all__ = [
	"ORGANIZATION_ADMIN_ROLES",
	"CreateOrganizationVersionService",
	"PublishOrganizationVersionService",
	"ReplaceOrganizationHierarchyService",
	"UpsertHospitalService",
	"create_organization_version",
	"get_organization_hierarchy",
	"publish_organization_version",
	"replace_organization_hierarchy",
	"upsert_hospital",
]
