from __future__ import annotations

from typing import cast

import frappe
from frappe.utils import now_datetime

from medops.common.domain_service import (
	DomainServiceDefinition,
	DomainServiceExecution,
	fingerprint_json,
)
from medops.common.master_data import (
	MASTER_DATA_TARGET_POLICIES,
	MasterDataContractError,
	MasterDataDomainUpsert,
	MasterDataFieldPolicy,
	MasterDataRequestReview,
	MasterDataRequestSubmit,
	MasterDataRequestUpsert,
	MasterDataTargetPolicy,
	normalize_policy_value,
	serialize_stored_value,
)
from medops.hrp_master_data.doctype.hrp_master_data_domain.hrp_master_data_domain import (
	HRPMasterDataDomain,
)
from medops.hrp_master_data.doctype.hrp_master_data_request.hrp_master_data_request import (
	HRPMasterDataRequest,
)
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.domain_service import DomainService
from medops.services.errors import (
	raise_ione_error,
	require_authenticated_user,
)

MASTER_DATA_ADMIN_ROLES = frozenset({"System Manager", "HRP System Manager", "HRP Data Steward"})
MASTER_DATA_REQUESTER_ROLES = frozenset({*MASTER_DATA_ADMIN_ROLES, "HRP User"})
MASTER_DATA_REVIEWER_ROLES = MASTER_DATA_ADMIN_ROLES


def _execution_payload(execution: DomainServiceExecution) -> dict[str, object]:
	return {
		**execution.result,
		"correlation_id": execution.correlation_id,
		"request_id": execution.request_id,
		"idempotency_replayed": execution.idempotency_replayed,
	}


def _get_domain(name: str) -> HRPMasterDataDomain:
	if not frappe.db.exists("HRP Master Data Domain", name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(HRPMasterDataDomain, frappe.get_doc("HRP Master Data Domain", name))


def _get_request(name: str) -> HRPMasterDataRequest:
	if not frappe.db.exists("HRP Master Data Request", name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(HRPMasterDataRequest, frappe.get_doc("HRP Master Data Request", name))


def _assert_domain_operational(domain: HRPMasterDataDomain) -> MasterDataTargetPolicy:
	if not bool(domain.enabled):
		raise_ione_error("INVALID_STATE_TRANSITION")
	policy = MASTER_DATA_TARGET_POLICIES.get(domain.target_doctype)
	if (
		policy is None
		or int(domain.policy_version or 0) != policy.version
		or domain.policy_digest != policy.digest
	):
		raise_ione_error("CONFIGURATION_INVALID")
	return policy


def _assert_organization_scope(
	*,
	company: str,
	hospital: str,
	organization_unit: str,
	effective_on: str,
) -> None:
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
		FOR UPDATE
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


def _target_snapshot(
	policy: MasterDataTargetPolicy,
	target_name: str,
) -> dict[str, object]:
	if not frappe.db.exists(policy.target_doctype, target_name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	field_names = [field.field_name for field in policy.fields]
	if policy.company_field and policy.company_field not in field_names:
		field_names.append(policy.company_field)
	# Both the table and selected columns come only from the static policy registry.
	columns = ", ".join(f"`{field}`" for field in ("modified", *field_names))
	rows = frappe.db.sql(
		f"""
		SELECT {columns}
		FROM `tab{policy.target_doctype}`
		WHERE name = %s
		FOR UPDATE
		""",  # nosec B608
		target_name,
		as_dict=True,
	)
	if not rows:
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(dict[str, object], rows[0])


def _validate_link_value(
	field_policy: MasterDataFieldPolicy,
	proposed_value: str,
) -> None:
	if (
		field_policy.value_type == "Link"
		and proposed_value
		and field_policy.link_doctype
		and not frappe.db.exists(field_policy.link_doctype, proposed_value)
	):
		raise_ione_error("RESOURCE_NOT_FOUND")


def _normalized_change_rows(
	command: MasterDataRequestUpsert,
	policy: MasterDataTargetPolicy,
) -> tuple[list[dict[str, object]], str | None]:
	operation_allowed = {
		"Create": policy.allow_create,
		"Update": policy.allow_update,
		"Disable": policy.allow_disable,
	}
	if not operation_allowed[command.operation]:
		raise_ione_error("OPERATION_NOT_ALLOWED")

	snapshot: dict[str, object] | None = None
	baseline_modified_at: str | None = None
	if command.target_name:
		snapshot = _target_snapshot(policy, command.target_name)
		baseline_modified_at = str(snapshot["modified"])
		if policy.company_field and snapshot.get(policy.company_field) != command.company:
			raise_ione_error("CONFLICT")

	fields_by_name = policy.fields_by_name
	change_names = {change.field_name for change in command.changes}
	if not change_names.issubset(fields_by_name):
		raise_ione_error("OPERATION_NOT_ALLOWED")
	if command.operation == "Create":
		required = {field.field_name for field in policy.fields if field.required_on_create}
		if not required.issubset(change_names):
			raise_ione_error("INVALID_REQUEST")
	if command.operation == "Disable":
		if change_names != {"disabled"}:
			raise_ione_error("INVALID_REQUEST")

	rows: list[dict[str, object]] = []
	for sequence_no, change in enumerate(command.changes, start=1):
		field_policy = fields_by_name[change.field_name]
		try:
			proposed_value = normalize_policy_value(
				field_policy,
				change.proposed_value,
				allow_empty=not (command.operation == "Create" and field_policy.required_on_create),
			)
		except MasterDataContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		_validate_link_value(field_policy, proposed_value)
		if command.operation == "Disable" and proposed_value != "1":
			raise_ione_error("INVALID_REQUEST")
		current_value = (
			serialize_stored_value(snapshot.get(change.field_name)) if snapshot is not None else ""
		)
		if command.operation != "Create" and current_value == proposed_value:
			raise_ione_error("CONFLICT")
		rows.append(
			{
				"sequence_no": sequence_no,
				"field_name": change.field_name,
				"field_label": field_policy.label,
				"value_type": field_policy.value_type,
				"current_value": current_value,
				"proposed_value": proposed_value,
				"reason": change.reason,
			}
		)
	return rows, baseline_modified_at


def _proposal_digest(
	command: MasterDataRequestUpsert,
	*,
	domain: HRPMasterDataDomain,
	rows: list[dict[str, object]],
	baseline_modified_at: str | None,
) -> str:
	return fingerprint_json(
		{
			"schema_version": 1,
			"master_data_domain": domain.name,
			"policy_digest": domain.policy_digest,
			"company": command.company,
			"hospital": command.hospital,
			"organization_unit": command.organization_unit,
			"operation": command.operation,
			"target_name": command.target_name,
			"subject": command.subject,
			"effective_on": command.effective_on,
			"baseline_modified_at": baseline_modified_at,
			"changes": rows,
		}
	)


def _assert_request_owner(request: HRPMasterDataRequest) -> None:
	if MASTER_DATA_ADMIN_ROLES.intersection(frappe.get_roles()):
		return
	if request.requested_by != frappe.session.user:
		raise_ione_error("PERMISSION_DENIED")


class UpsertMasterDataDomainService(DomainService[MasterDataDomainUpsert]):
	definition = DomainServiceDefinition(
		name="hrp_master_data.domain.upsert",
		version=1,
		kind="command",
		required_roles=MASTER_DATA_ADMIN_ROLES,
	)

	def request_payload(self, command: MasterDataDomainUpsert) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: MasterDataDomainUpsert) -> None:
		if not frappe.db.exists("DocType", command.target_doctype):
			raise_ione_error("CONFIGURATION_INVALID")

	def perform(self, command: MasterDataDomainUpsert) -> dict[str, object]:
		exists = bool(frappe.db.exists("HRP Master Data Domain", command.code))
		if exists != (command.expected_revision > 0):
			raise_ione_error("CONFLICT")
		if command.expected_revision == 0:
			if frappe.db.exists(
				"HRP Master Data Domain",
				{"target_doctype": command.target_doctype},
			):
				raise_ione_error("CONFLICT")
			doc = cast(
				HRPMasterDataDomain,
				frappe.get_doc(
					{
						"doctype": "HRP Master Data Domain",
						"code": command.code,
						"display_name": command.display_name,
						"target_doctype": command.target_doctype,
						"enabled": int(command.enabled),
						"revision": 1,
						"remarks": command.remarks,
					}
				),
			)
			doc.flags.master_data_service_write = True
			doc.insert(ignore_permissions=True)
			emit_audit_event(
				"master_data_domain_created",
				logger_name="medops.master_data",
				revision=1,
				policy_version=doc.policy_version,
				policy_digest=doc.policy_digest,
			)
			return {**doc.as_public_dict(), "changed": True, "changed_fields": ["created"]}

		current_revision = HRPMasterDataDomain.lock_revision(
			command.expected_revision,
			command.code,
		)
		doc = _get_domain(command.code)
		if doc.target_doctype != command.target_doctype:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		changed_fields = [
			fieldname
			for fieldname in ("display_name", "enabled", "remarks")
			if (
				bool(doc.get(fieldname))
				if fieldname == "enabled"
				else (str(doc.get(fieldname)) if doc.get(fieldname) else None)
			)
			!= (command.enabled if fieldname == "enabled" else getattr(command, fieldname))
		]
		policy = MASTER_DATA_TARGET_POLICIES[command.target_doctype]
		if int(doc.policy_version or 0) != policy.version or doc.policy_digest != policy.digest:
			changed_fields.append("policy")
		if not changed_fields:
			return {**doc.as_public_dict(), "changed": False, "changed_fields": []}
		doc.display_name = command.display_name
		doc.enabled = int(command.enabled)
		doc.remarks = command.remarks
		doc.flags.master_data_service_write = True
		doc.flags.locked_revision = current_revision
		doc.save(ignore_permissions=True)
		emit_audit_event(
			"master_data_domain_changed",
			logger_name="medops.master_data",
			before_revision=current_revision,
			after_revision=doc.revision,
			changed_field_count=len(changed_fields),
			changed_fields=",".join(changed_fields),
			policy_version=doc.policy_version,
			policy_digest=doc.policy_digest,
		)
		return {
			**doc.as_public_dict(),
			"changed": True,
			"changed_fields": changed_fields,
		}


def upsert_master_data_domain(
	command: MasterDataDomainUpsert,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		UpsertMasterDataDomainService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


class SaveMasterDataRequestService(DomainService[MasterDataRequestUpsert]):
	definition = DomainServiceDefinition(
		name="hrp_master_data.request.save",
		version=1,
		kind="command",
		required_roles=MASTER_DATA_REQUESTER_ROLES,
	)

	def request_payload(self, command: MasterDataRequestUpsert) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: MasterDataRequestUpsert) -> None:
		if not frappe.db.exists("HRP Master Data Domain", command.master_data_domain):
			raise_ione_error("RESOURCE_NOT_FOUND")
		if not frappe.db.exists("Company", command.company):
			raise_ione_error("RESOURCE_NOT_FOUND")
		if command.request_name:
			request = _get_request(command.request_name)
			_assert_request_owner(request)
			if request.docstatus != 0 or request.request_status != "Draft":
				raise_ione_error("INVALID_STATE_TRANSITION")

	def perform(self, command: MasterDataRequestUpsert) -> dict[str, object]:
		domain = _get_domain(command.master_data_domain)
		policy = _assert_domain_operational(domain)
		_assert_organization_scope(
			company=command.company,
			hospital=command.hospital,
			organization_unit=command.organization_unit,
			effective_on=command.effective_on,
		)
		rows, baseline_modified_at = _normalized_change_rows(command, policy)
		digest = _proposal_digest(
			command,
			domain=domain,
			rows=rows,
			baseline_modified_at=baseline_modified_at,
		)

		if not command.request_name:
			doc = cast(
				HRPMasterDataRequest,
				frappe.get_doc(
					{
						"doctype": "HRP Master Data Request",
						"master_data_domain": domain.name,
						"target_doctype": domain.target_doctype,
						"company": command.company,
						"hospital": command.hospital,
						"organization_unit": command.organization_unit,
						"operation": command.operation,
						"target_name": command.target_name,
						"subject": command.subject,
						"effective_on": command.effective_on,
						"request_status": "Draft",
						"requested_by": frappe.session.user,
						"requested_at": now_datetime(),
						"proposal_digest": digest,
						"baseline_modified_at": baseline_modified_at,
						"revision": 1,
						"changes": rows,
					}
				),
			)
			doc.flags.master_data_service_write = True
			doc.insert(ignore_permissions=True)
			emit_audit_event(
				"master_data_request_created",
				logger_name="medops.master_data",
				revision=1,
				change_count=len(rows),
				proposal_digest=digest,
				operation=command.operation,
			)
			return {**doc.as_public_dict(), "changed": True}

		current_revision = HRPMasterDataRequest.lock_revision(
			command.expected_revision,
			command.request_name,
		)
		doc = _get_request(command.request_name)
		_assert_request_owner(doc)
		if doc.docstatus != 0 or doc.request_status != "Draft":
			raise_ione_error("INVALID_STATE_TRANSITION")
		if (
			doc.proposal_digest == digest
			and doc.master_data_domain == domain.name
			and doc.target_doctype == domain.target_doctype
		):
			return {**doc.as_public_dict(), "changed": False}
		doc.master_data_domain = domain.name
		doc.target_doctype = domain.target_doctype
		doc.company = command.company
		doc.hospital = command.hospital
		doc.organization_unit = command.organization_unit
		doc.operation = command.operation
		doc.target_name = command.target_name
		doc.subject = command.subject
		doc.effective_on = command.effective_on
		doc.proposal_digest = digest
		doc.baseline_modified_at = baseline_modified_at
		doc.set("changes", rows)
		doc.flags.master_data_service_write = True
		doc.flags.locked_revision = current_revision
		doc.save(ignore_permissions=True)
		emit_audit_event(
			"master_data_request_changed",
			logger_name="medops.master_data",
			before_revision=current_revision,
			after_revision=doc.revision,
			change_count=len(rows),
			proposal_digest=digest,
			operation=command.operation,
		)
		return {**doc.as_public_dict(), "changed": True}


def save_master_data_request(
	command: MasterDataRequestUpsert,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		SaveMasterDataRequestService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def _stored_command(request: HRPMasterDataRequest) -> MasterDataRequestUpsert:
	from medops.common.master_data import build_master_data_request_upsert

	try:
		return build_master_data_request_upsert(
			request_name=request.name,
			master_data_domain=request.master_data_domain,
			company=request.company,
			hospital=request.hospital,
			organization_unit=request.organization_unit,
			operation=request.operation,
			target_name=request.target_name,
			subject=request.subject,
			effective_on=request.effective_on,
			changes=[
				{
					"field_name": row.field_name,
					"proposed_value": row.proposed_value,
					"reason": row.reason,
				}
				for row in request.changes
			],
			expected_revision=request.revision,
		)
	except MasterDataContractError as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)


def _assert_proposal_current(
	request: HRPMasterDataRequest,
	*,
	domain: HRPMasterDataDomain,
) -> None:
	command = _stored_command(request)
	policy = _assert_domain_operational(domain)
	_assert_organization_scope(
		company=command.company,
		hospital=command.hospital,
		organization_unit=command.organization_unit,
		effective_on=command.effective_on,
	)
	rows, baseline_modified_at = _normalized_change_rows(command, policy)
	digest = _proposal_digest(
		command,
		domain=domain,
		rows=rows,
		baseline_modified_at=baseline_modified_at,
	)
	if digest != request.proposal_digest or (baseline_modified_at or None) != (
		str(request.baseline_modified_at) if request.baseline_modified_at else None
	):
		raise_ione_error("CONFLICT")


class SubmitMasterDataRequestService(DomainService[MasterDataRequestSubmit]):
	definition = DomainServiceDefinition(
		name="hrp_master_data.request.submit",
		version=1,
		kind="command",
		required_roles=MASTER_DATA_REQUESTER_ROLES,
	)

	def request_payload(self, command: MasterDataRequestSubmit) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: MasterDataRequestSubmit) -> None:
		request = _get_request(command.request_name)
		_assert_request_owner(request)
		if request.docstatus != 0 or request.request_status != "Draft":
			raise_ione_error("INVALID_STATE_TRANSITION")

	def perform(self, command: MasterDataRequestSubmit) -> dict[str, object]:
		current_revision = HRPMasterDataRequest.lock_revision(
			command.expected_revision,
			command.request_name,
		)
		request = _get_request(command.request_name)
		_assert_request_owner(request)
		if request.docstatus != 0 or request.request_status != "Draft":
			raise_ione_error("INVALID_STATE_TRANSITION")
		domain = _get_domain(request.master_data_domain)
		_assert_proposal_current(request, domain=domain)
		if request.target_name:
			pending = frappe.db.sql(
				"""
				SELECT name
				FROM `tabHRP Master Data Request`
				WHERE master_data_domain = %s
					AND target_name = %s
					AND request_status = 'Pending Review'
					AND docstatus = 1
					AND name != %s
				LIMIT 1
				FOR UPDATE
				""",
				(request.master_data_domain, request.target_name, request.name),
			)
			if pending:
				raise_ione_error("CONFLICT")
		request.request_status = "Pending Review"
		request.submitted_at = now_datetime()
		request.flags.master_data_service_write = True
		request.flags.locked_revision = current_revision
		request.flags.ignore_permissions = True
		request.submit()
		emit_audit_event(
			"master_data_request_submitted",
			logger_name="medops.master_data",
			before_revision=current_revision,
			after_revision=request.revision,
			change_count=len(request.changes),
			proposal_digest=request.proposal_digest,
			operation=request.operation,
		)
		return request.as_public_dict()


def submit_master_data_request(
	command: MasterDataRequestSubmit,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		SubmitMasterDataRequestService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


class ReviewMasterDataRequestService(DomainService[MasterDataRequestReview]):
	definition = DomainServiceDefinition(
		name="hrp_master_data.request.review",
		version=1,
		kind="command",
		required_roles=MASTER_DATA_REVIEWER_ROLES,
	)

	def request_payload(self, command: MasterDataRequestReview) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: MasterDataRequestReview) -> None:
		request = _get_request(command.request_name)
		if request.docstatus != 1 or request.request_status != "Pending Review":
			raise_ione_error("INVALID_STATE_TRANSITION")
		if request.requested_by == frappe.session.user:
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def perform(self, command: MasterDataRequestReview) -> dict[str, object]:
		current_revision = HRPMasterDataRequest.lock_revision(
			command.expected_revision,
			command.request_name,
		)
		request = _get_request(command.request_name)
		if request.docstatus != 1 or request.request_status != "Pending Review":
			raise_ione_error("INVALID_STATE_TRANSITION")
		if request.requested_by == frappe.session.user:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if command.decision == "Approve":
			domain = _get_domain(request.master_data_domain)
			_assert_proposal_current(request, domain=domain)
			request.request_status = "Approved"
		else:
			request.request_status = "Rejected"
		request.reviewed_by = frappe.session.user
		request.reviewed_at = now_datetime()
		request.decision_reason = command.reason
		request.flags.master_data_service_write = True
		request.flags.locked_revision = current_revision
		request.save(ignore_permissions=True)
		emit_audit_event(
			"master_data_request_reviewed",
			logger_name="medops.master_data",
			before_revision=current_revision,
			after_revision=request.revision,
			decision=command.decision,
			change_count=len(request.changes),
			proposal_digest=request.proposal_digest,
			operation=request.operation,
		)
		return request.as_public_dict()


def review_master_data_request(
	command: MasterDataRequestReview,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		ReviewMasterDataRequestService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def get_master_data_request(
	*,
	request_name: object,
	correlation_id: object | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		if not MASTER_DATA_REQUESTER_ROLES.intersection(frappe.get_roles()):
			raise_ione_error("PERMISSION_DENIED")
		if not isinstance(request_name, str) or not request_name.strip():
			raise_ione_error("INVALID_REQUEST")
		request = _get_request(request_name)
		_assert_request_owner(request)
		emit_audit_event(
			"master_data_request_read",
			logger_name="medops.master_data",
			revision=request.revision,
			request_status=request.request_status,
			change_count=len(request.changes),
			operation=request.operation,
		)
		return request.as_public_dict()


__all__ = [
	"MASTER_DATA_ADMIN_ROLES",
	"MASTER_DATA_REQUESTER_ROLES",
	"MASTER_DATA_REVIEWER_ROLES",
	"ReviewMasterDataRequestService",
	"SaveMasterDataRequestService",
	"SubmitMasterDataRequestService",
	"UpsertMasterDataDomainService",
	"get_master_data_request",
	"review_master_data_request",
	"save_master_data_request",
	"submit_master_data_request",
	"upsert_master_data_domain",
]
