from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import TypedDict, cast

import frappe
from frappe.model.document import Document

from medops.common.approval_matrix import (
	ApprovalEvaluation,
	ApprovalMatrixDefinition,
	ApprovalMatrixStep,
	ApprovalMatrixUpsert,
	ResolvedApprover,
	active_steps,
	build_approval_decision,
)
from medops.common.domain_service import DomainServiceDefinition, DomainServiceExecution
from medops.hrp_workflow_authorization.doctype.hrp_approval_matrix.hrp_approval_matrix import (
	HRPApprovalMatrix,
)
from medops.hrp_workflow_authorization.permissions import has_scoped_permission
from medops.hrp_workflow_authorization.services.access_scope import dimension_fields_for
from medops.hrp_workflow_authorization.services.delegation import apply_active_delegations
from medops.services.audit_context import emit_audit_event
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_roles

APPROVAL_MATRIX_ADMIN_ROLES = frozenset({"System Manager", "HRP System Manager"})
APPROVAL_MATRIX_EVALUATE_ROLES = frozenset(
	{
		*APPROVAL_MATRIX_ADMIN_ROLES,
		"HRP User",
		"HRP Auditor",
		"HRP Integration User",
	}
)
MAX_MATRIX_CANDIDATES = 100


class ApprovalMatrixDocumentPayload(TypedDict):
	doctype: str
	code: str
	display_name: str
	target_doctype: str
	company: str
	hospital: str
	organization_unit: str | None
	include_descendants: int
	priority: int
	dimensions_json: str
	enabled: int
	valid_from: str
	valid_to: str | None
	revision: int
	remarks: str | None
	steps: list[dict[str, object]]


def _execution_payload(execution: DomainServiceExecution) -> dict[str, object]:
	return {
		**execution.result,
		"correlation_id": execution.correlation_id,
		"request_id": execution.request_id,
		"idempotency_replayed": execution.idempotency_replayed,
	}


def _matrix_doc(name: str) -> HRPApprovalMatrix:
	if not frappe.db.exists("HRP Approval Matrix", name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(HRPApprovalMatrix, frappe.get_doc("HRP Approval Matrix", name))


def _assert_organization_scope(
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


class UpsertApprovalMatrixService(DomainService[ApprovalMatrixUpsert]):
	definition = DomainServiceDefinition(
		name="hrp_workflow_authorization.approval_matrix.upsert",
		version=1,
		kind="command",
		required_roles=APPROVAL_MATRIX_ADMIN_ROLES,
	)

	def request_payload(self, command: ApprovalMatrixUpsert) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: ApprovalMatrixUpsert) -> None:
		definition = command.definition
		for doctype, name in (
			("Company", definition.company),
			("DocType", definition.target_doctype),
			("HRP Hospital", definition.hospital),
		):
			if not frappe.db.exists(doctype, name):
				raise_ione_error("RESOURCE_NOT_FOUND")
		if command.matrix_name and not frappe.db.exists("HRP Approval Matrix", command.matrix_name):
			raise_ione_error("RESOURCE_NOT_FOUND")

	def perform(self, command: ApprovalMatrixUpsert) -> dict[str, object]:
		definition = command.definition
		_assert_organization_scope(
			company=definition.company,
			hospital=definition.hospital,
			organization_unit=definition.organization_unit,
			effective_on=definition.valid_from,
		)
		if command.matrix_name is None:
			if command.expected_revision != 0 or frappe.db.exists("HRP Approval Matrix", definition.code):
				raise_ione_error("CONFLICT")
			doc = cast(
				HRPApprovalMatrix,
				frappe.get_doc(self._document_payload(definition)),
			)
			try:
				doc.insert(ignore_permissions=True)
			except (frappe.DuplicateEntryError, frappe.UniqueValidationError) as exc:
				raise_ione_error("CONFLICT", cause=exc)
			emit_audit_event(
				"approval_matrix_created",
				logger_name="medops.approval_matrix",
				policy_digest=doc.policy_digest,
				revision=1,
				step_count=len(doc.steps),
			)
			return {**doc.as_public_dict(), "changed": True, "changed_fields": ["created"]}

		current_revision = HRPApprovalMatrix.lock_revision(
			command.expected_revision,
			command.matrix_name,
		)
		doc = _matrix_doc(command.matrix_name)
		identity = (doc.code, doc.target_doctype, doc.company, doc.hospital, doc.organization_unit or None)
		requested = (
			definition.code,
			definition.target_doctype,
			definition.company,
			definition.hospital,
			definition.organization_unit,
		)
		if identity != requested:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		changed_fields = [
			fieldname
			for fieldname, current, requested_value in (
				("display_name", doc.display_name, definition.display_name),
				("include_descendants", bool(doc.include_descendants), definition.include_descendants),
				("priority", int(doc.priority), definition.priority),
				(
					"dimensions_json",
					doc.dimensions_json or "{}",
					json.dumps(
						definition.dimensions, ensure_ascii=False, separators=(",", ":"), sort_keys=True
					),
				),
				("enabled", bool(doc.enabled), definition.enabled),
				("valid_from", str(doc.valid_from), definition.valid_from),
				("valid_to", str(doc.valid_to) if doc.valid_to else None, definition.valid_to),
				("remarks", doc.remarks or None, definition.remarks),
				("steps", self._stored_steps(doc), self._requested_steps(definition)),
			)
			if current != requested_value
		]
		if not changed_fields:
			return {**doc.as_public_dict(), "changed": False, "changed_fields": []}
		payload = self._document_payload(definition)
		for fieldname in (
			"display_name",
			"include_descendants",
			"priority",
			"dimensions_json",
			"enabled",
			"valid_from",
			"valid_to",
			"remarks",
		):
			doc.set(fieldname, payload[fieldname])
		doc.set("steps", [])
		for step in payload["steps"]:
			doc.append("steps", step)
		doc.flags.locked_revision = current_revision
		doc.save(ignore_permissions=True)
		emit_audit_event(
			"approval_matrix_changed",
			logger_name="medops.approval_matrix",
			policy_digest=doc.policy_digest,
			before_revision=current_revision,
			after_revision=doc.revision,
			changed_field_count=len(changed_fields),
			changed_fields=",".join(changed_fields),
		)
		return {**doc.as_public_dict(), "changed": True, "changed_fields": changed_fields}

	@staticmethod
	def _document_payload(definition: ApprovalMatrixDefinition) -> ApprovalMatrixDocumentPayload:
		return {
			"doctype": "HRP Approval Matrix",
			"code": definition.code,
			"display_name": definition.display_name,
			"target_doctype": definition.target_doctype,
			"company": definition.company,
			"hospital": definition.hospital,
			"organization_unit": definition.organization_unit,
			"include_descendants": int(definition.include_descendants),
			"priority": definition.priority,
			"dimensions_json": json.dumps(
				definition.dimensions,
				ensure_ascii=False,
				separators=(",", ":"),
				sort_keys=True,
			),
			"enabled": int(definition.enabled),
			"valid_from": definition.valid_from,
			"valid_to": definition.valid_to,
			"revision": 1,
			"remarks": definition.remarks,
			"steps": [step.as_dict() for step in definition.steps],
		}

	@staticmethod
	def _stored_steps(doc: HRPApprovalMatrix) -> list[dict[str, object]]:
		return sorted(
			[
				{
					"sequence_no": int(row.sequence_no),
					"step_name": row.step_name,
					"threshold_amount": float(row.threshold_amount),
					"approver_type": row.approver_type,
					"approver_role": row.approver_role or None,
					"approver_user": row.approver_user or None,
					"approval_mode": row.approval_mode,
					"description": row.description or None,
				}
				for row in doc.steps
			],
			key=UpsertApprovalMatrixService._step_sort_key,
		)

	@staticmethod
	def _requested_steps(definition: ApprovalMatrixDefinition) -> list[dict[str, object]]:
		return sorted(
			[step.as_dict() for step in definition.steps],
			key=UpsertApprovalMatrixService._step_sort_key,
		)

	@staticmethod
	def _step_sort_key(item: dict[str, object]) -> tuple[int, str, str]:
		return (
			int(str(item["sequence_no"])),
			str(item["approver_type"]),
			str(item["approver_role"] or item["approver_user"]),
		)


def upsert_approval_matrix(
	command: ApprovalMatrixUpsert,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		UpsertApprovalMatrixService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def _matrix_matches_dimensions(matrix: HRPApprovalMatrix, dimensions: dict[str, str]) -> bool:
	try:
		policy = json.loads(matrix.dimensions_json or "{}")
	except json.JSONDecodeError as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)
	if not isinstance(policy, dict) or any(
		not isinstance(key, str) or not isinstance(value, str) for key, value in policy.items()
	):
		raise_ione_error("CONFIGURATION_INVALID")
	return policy == dimensions


def _matrix_matches_unit(matrix: HRPApprovalMatrix, organization_unit: str) -> bool:
	if not matrix.organization_unit:
		return True
	if matrix.organization_unit == organization_unit:
		return True
	if not bool(matrix.include_descendants):
		return False
	root = frappe.db.get_value(
		"HRP Organization Unit",
		matrix.organization_unit,
		["organization_version", "lft", "rgt"],
		as_dict=True,
	)
	target = frappe.db.get_value(
		"HRP Organization Unit",
		organization_unit,
		["organization_version", "lft", "rgt"],
		as_dict=True,
	)
	if not root or not target or root.organization_version != target.organization_version:
		return False
	if any(value is None for value in (root.lft, root.rgt, target.lft, target.rgt)):
		return False
	return int(root.lft) <= int(target.lft) and int(target.rgt) <= int(root.rgt)


def _select_matrix(command: ApprovalEvaluation) -> HRPApprovalMatrix:
	rows = frappe.get_all(
		"HRP Approval Matrix",
		filters={
			"target_doctype": command.target_doctype,
			"company": command.company,
			"hospital": command.hospital,
			"enabled": 1,
			"valid_from": ("<=", command.effective_on),
		},
		fields=["name"],
		order_by="priority desc, revision desc, name asc",
		limit=MAX_MATRIX_CANDIDATES + 1,
	)
	if len(rows) > MAX_MATRIX_CANDIDATES:
		raise_ione_error("CONFIGURATION_INVALID")
	candidates: list[HRPApprovalMatrix] = []
	for row in rows:
		matrix = _matrix_doc(str(row.name))
		if matrix.valid_to and str(matrix.valid_to) < command.effective_on:
			continue
		if not _matrix_matches_unit(matrix, command.organization_unit):
			continue
		if not _matrix_matches_dimensions(matrix, command.dimensions):
			continue
		candidates.append(matrix)
	if not candidates:
		raise_ione_error("RESOURCE_NOT_FOUND")
	highest_priority = max(int(matrix.priority) for matrix in candidates)
	top = [matrix for matrix in candidates if int(matrix.priority) == highest_priority]
	if len(top) != 1:
		raise_ione_error("CONFIGURATION_INVALID")
	return top[0]


def _can_approve(user: str, target_doc: Document) -> bool:
	target_doctype = target_doc.doctype
	return bool(
		frappe.has_permission(target_doctype, ptype="write", doc=target_doc, user=user)
		and has_scoped_permission(target_doc, user=user, ptype="write")
	)


def _role_approvers(role: str, target_doc: Document) -> list[ResolvedApprover]:
	users = frappe.get_all(
		"Has Role",
		filters={"role": role, "parenttype": "User"},
		pluck="parent",
		order_by="parent asc",
	)
	result: list[ResolvedApprover] = []
	for user in users:
		state = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
		if not state or not bool(state.enabled) or state.user_type != "System User":
			continue
		if _can_approve(str(user), target_doc):
			result.append({"user": str(user), "source_type": "Role", "source_value": role})
	return result


def _resolve_approvers(step: ApprovalMatrixStep, target_doc: Document) -> list[ResolvedApprover]:
	if step.approver_type == "Role":
		return _role_approvers(str(step.approver_role), target_doc)
	user = str(step.approver_user)
	state = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
	if (
		not state
		or not bool(state.enabled)
		or state.user_type != "System User"
		or not _can_approve(user, target_doc)
	):
		return []
	return [{"user": user, "source_type": "User", "source_value": user}]


class EvaluateApprovalMatrixService(DomainService[ApprovalEvaluation]):
	definition = DomainServiceDefinition(
		name="hrp_workflow_authorization.approval_matrix.evaluate",
		version=1,
		kind="query",
		required_roles=APPROVAL_MATRIX_EVALUATE_ROLES,
	)

	def request_payload(self, command: ApprovalEvaluation) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: ApprovalEvaluation) -> None:
		if not frappe.db.exists("DocType", command.target_doctype):
			raise_ione_error("RESOURCE_NOT_FOUND")
		if not frappe.db.exists(command.target_doctype, command.docname):
			raise_ione_error("RESOURCE_NOT_FOUND")
		doc = frappe.get_doc(command.target_doctype, command.docname)
		if not frappe.has_permission(command.target_doctype, ptype="read", doc=doc):
			raise_ione_error("PERMISSION_DENIED")
		self._assert_document_context(command)
		_assert_organization_scope(
			company=command.company,
			hospital=command.hospital,
			organization_unit=command.organization_unit,
			effective_on=command.effective_on,
		)
		if not has_scoped_permission(doc, user=frappe.session.user, ptype="read"):
			raise_ione_error("PERMISSION_DENIED")

	@staticmethod
	def _assert_document_context(command: ApprovalEvaluation) -> None:
		doc = frappe.get_doc(command.target_doctype, command.docname)
		meta = frappe.get_meta(command.target_doctype)
		fields = {field.fieldname for field in meta.fields}
		dimension_fields = dimension_fields_for(command.target_doctype)
		for dimension, expected in (
			("company", command.company),
			("hospital", command.hospital),
			("organization_unit", command.organization_unit),
		):
			fieldname = dimension_fields.get(dimension)
			if fieldname is None or doc.get(fieldname) != expected:
				raise_ione_error("CONFLICT")
		for key, expected in command.dimensions.items():
			if key not in fields or str(doc.get(key) or "") != expected:
				raise_ione_error("CONFLICT")
		date_fields = (
			"transaction_date",
			"posting_date",
			"effective_on",
			"request_date",
			"valid_from",
			"date",
		)
		date_field = next((fieldname for fieldname in date_fields if fieldname in fields), None)
		stored_date = doc.get(date_field) if date_field else str(doc.creation or "")[:10]
		if not stored_date or str(stored_date) != command.effective_on:
			raise_ione_error("CONFLICT")
		amount_fields = (
			"grand_total",
			"rounded_total",
			"base_grand_total",
			"total_amount",
			"total",
		)
		available = next((fieldname for fieldname in amount_fields if fieldname in fields), None)
		if available is None:
			if command.amount != 0:
				raise_ione_error("CONFLICT")
			return
		try:
			stored = Decimal(str(doc.get(available) or 0)).quantize(Decimal("0.01"))
		except (InvalidOperation, ValueError) as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		if stored != command.amount:
			raise_ione_error("CONFLICT")

	def perform(self, command: ApprovalEvaluation) -> dict[str, object]:
		matrix = _select_matrix(command)
		definition = matrix.as_definition(revision=matrix.revision)
		steps = active_steps(definition, command.amount)
		if not steps:
			raise_ione_error("CONFIGURATION_INVALID")
		target_doc = frappe.get_doc(command.target_doctype, command.docname)
		resolved = [(step, _resolve_approvers(step, target_doc)) for step in steps]
		resolved = apply_active_delegations(
			matrix=matrix,
			target_doc=target_doc,
			resolved_rows=resolved,
		)
		try:
			decision = build_approval_decision(
				evaluation=command,
				matrix=matrix.name,
				definition=definition,
				resolved_rows=resolved,
			)
		except ValueError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		emit_audit_event(
			"approval_matrix_evaluated",
			logger_name="medops.approval_matrix",
			decision_digest=decision["decision_digest"],
			policy_digest=decision["policy_digest"],
			policy_version=decision["policy_version"],
			step_count=len(decision["steps"]),
			approver_count=len(decision["approvers"]),
			delegation_count=len(decision["delegations"]),
		)
		return cast(dict[str, object], decision)


def evaluate_approval_matrix(
	command: ApprovalEvaluation,
	*,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(EvaluateApprovalMatrixService().execute(command, correlation_id=correlation_id))


def get_approval_matrix(
	matrix_name: str,
	*,
	correlation_id: object | None = None,
) -> dict[str, object]:
	from medops.services.audit_context import service_audit_scope

	with service_audit_scope(correlation_id) as context:
		require_roles({*APPROVAL_MATRIX_ADMIN_ROLES, "HRP Auditor"})
		doc = _matrix_doc(matrix_name)
		emit_audit_event(
			"approval_matrix_read",
			logger_name="medops.approval_matrix",
			policy_digest=doc.policy_digest,
			revision=int(doc.revision),
		)
		return {
			**doc.as_public_dict(),
			"correlation_id": context.correlation_id,
			"request_id": context.request_id,
			"idempotency_replayed": False,
		}


__all__ = [
	"APPROVAL_MATRIX_ADMIN_ROLES",
	"APPROVAL_MATRIX_EVALUATE_ROLES",
	"EvaluateApprovalMatrixService",
	"UpsertApprovalMatrixService",
	"evaluate_approval_matrix",
	"get_approval_matrix",
	"upsert_approval_matrix",
]
