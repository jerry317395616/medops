from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime, nowdate

from medops.common.approval_matrix import ApprovalMatrixStep, ResolvedApprover
from medops.common.delegation import (
	DelegationCreate,
	DelegationGrant,
	DelegationRevoke,
	apply_delegation_grants,
)
from medops.common.domain_service import DomainServiceDefinition, DomainServiceExecution
from medops.hrp_workflow_authorization.doctype.hrp_approval_matrix.hrp_approval_matrix import (
	HRPApprovalMatrix,
)
from medops.hrp_workflow_authorization.doctype.hrp_delegation.hrp_delegation import HRPDelegation
from medops.hrp_workflow_authorization.services.access_scope import dimension_fields_for
from medops.hrp_workflow_authorization.services.approval_capability import (
	can_approve_target,
	can_delegate_target_scope,
)
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_roles

DELEGATION_ADMIN_ROLES = frozenset({"System Manager", "HRP System Manager"})
DELEGATION_CREATE_ROLES = frozenset({*DELEGATION_ADMIN_ROLES, "HRP Department Manager"})
DELEGATION_READ_ROLES = frozenset({*DELEGATION_CREATE_ROLES, "HRP Auditor", "HRP User"})
MAX_DELEGATION_CANDIDATES = 100


@dataclass(slots=True)
class _ScopeContext:
	doctype: str
	values: dict[str, object]

	def get(self, key: str) -> object:
		return self.values.get(key)


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


def _lock_matrix(name: str) -> HRPApprovalMatrix:
	rows = frappe.db.sql(
		"SELECT name FROM `tabHRP Approval Matrix` WHERE name = %s FOR UPDATE",
		name,
		as_dict=True,
	)
	if not rows:
		raise_ione_error("RESOURCE_NOT_FOUND")
	return _matrix_doc(name)


def _delegation_doc(name: str, *, lock: bool = False) -> HRPDelegation:
	if lock:
		rows = frappe.db.sql(
			"SELECT name FROM `tabHRP Delegation` WHERE name = %s FOR UPDATE",
			name,
			as_dict=True,
		)
		if not rows:
			raise_ione_error("RESOURCE_NOT_FOUND")
	elif not frappe.db.exists("HRP Delegation", name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(HRPDelegation, frappe.get_doc("HRP Delegation", name))


def _user_is_active(user: str) -> bool:
	row = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
	return bool(row and row.enabled and row.user_type == "System User")


def _has_scope(user: str, matrix: HRPApprovalMatrix) -> bool:
	dimensions = dimension_fields_for(matrix.target_doctype)
	logical_values = {
		"company": matrix.company,
		"hospital": matrix.hospital,
		"organization_unit": matrix.organization_unit,
	}
	context = _ScopeContext(
		doctype=matrix.target_doctype,
		values={fieldname: logical_values[dimension] for dimension, fieldname in dimensions.items()},
	)
	return can_delegate_target_scope(user, context)


def _owns_matrix_step(user: str, matrix: HRPApprovalMatrix, step_sequence: int | None) -> bool:
	roles = set(frappe.get_roles(user))
	for row in matrix.steps:
		if step_sequence is not None and int(row.sequence_no) != step_sequence:
			continue
		if row.approver_type == "User" and row.approver_user == user:
			return True
		if row.approver_type == "Role" and row.approver_role in roles:
			return True
	return False


def _steps_overlap(left: int | None, right: int | None) -> bool:
	return left is None or right is None or left == right


def _assert_no_conflict(command: DelegationCreate, matrix: HRPApprovalMatrix) -> None:
	rows = frappe.get_all(
		"HRP Delegation",
		filters={
			"matrix": matrix.name,
			"status": ("in", ["Scheduled", "Active"]),
			"valid_from": ("<=", command.valid_to),
			"valid_to": (">=", command.valid_from),
		},
		fields=["from_user", "to_user", "step_sequence"],
		limit=MAX_DELEGATION_CANDIDATES + 1,
	)
	if len(rows) > MAX_DELEGATION_CANDIDATES:
		raise_ione_error("CONFIGURATION_INVALID")
	adjacency: dict[str, set[str]] = {}
	for row in rows:
		sequence = int(row.step_sequence) if row.step_sequence else None
		if not _steps_overlap(sequence, command.step_sequence):
			continue
		adjacency.setdefault(str(row.from_user), set()).add(str(row.to_user))
		if row.from_user == command.from_user or row.to_user == command.to_user:
			raise_ione_error("CONFLICT")
		if row.to_user == command.from_user:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if row.from_user == command.to_user:
			raise_ione_error("OPERATION_NOT_ALLOWED")
	stack = [command.to_user]
	visited: set[str] = set()
	while stack:
		current = stack.pop()
		if current == command.from_user:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if current in visited:
			continue
		visited.add(current)
		stack.extend(adjacency.get(current, ()))


class CreateDelegationService(DomainService[DelegationCreate]):
	definition = DomainServiceDefinition(
		name="hrp_workflow_authorization.delegation.create",
		version=1,
		kind="command",
		required_roles=DELEGATION_CREATE_ROLES,
	)

	def authorize(self, command: DelegationCreate) -> None:
		require_roles(self.definition.required_roles)
		if not DELEGATION_ADMIN_ROLES.intersection(frappe.get_roles()):
			if command.from_user != frappe.session.user:
				raise_ione_error("PERMISSION_DENIED")

	def request_payload(self, command: DelegationCreate) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: DelegationCreate) -> None:
		matrix = _matrix_doc(command.matrix)
		today = nowdate()
		if command.valid_from < today or command.valid_to < today:
			raise_ione_error("INVALID_REQUEST")
		if not bool(matrix.enabled):
			raise_ione_error("INVALID_STATE_TRANSITION")
		if command.valid_from < str(matrix.valid_from):
			raise_ione_error("CONFLICT")
		if matrix.valid_to and command.valid_to > str(matrix.valid_to):
			raise_ione_error("CONFLICT")
		if not _user_is_active(command.from_user) or not _user_is_active(command.to_user):
			raise_ione_error("INVALID_STATE_TRANSITION")
		if not _owns_matrix_step(command.from_user, matrix, command.step_sequence):
			raise_ione_error("PERMISSION_DENIED")
		if not _has_scope(command.from_user, matrix) or not _has_scope(command.to_user, matrix):
			raise_ione_error("PERMISSION_DENIED")

	def perform(self, command: DelegationCreate) -> dict[str, object]:
		matrix = _lock_matrix(command.matrix)
		_assert_no_conflict(command, matrix)
		status = "Active" if command.valid_from <= nowdate() else "Scheduled"
		doc = cast(
			HRPDelegation,
			frappe.get_doc(
				{
					"doctype": "HRP Delegation",
					"matrix": matrix.name,
					"matrix_revision": int(matrix.revision),
					"matrix_digest": matrix.policy_digest,
					"target_doctype": matrix.target_doctype,
					"company": matrix.company,
					"hospital": matrix.hospital,
					"organization_unit": matrix.organization_unit,
					"include_descendants": int(matrix.include_descendants),
					"from_user": command.from_user,
					"to_user": command.to_user,
					"valid_from": command.valid_from,
					"valid_to": command.valid_to,
					"step_sequence": command.step_sequence,
					"reason": command.reason,
					"status": status,
				}
			),
		)
		doc.flags.delegation_service_write = True
		doc.insert(ignore_permissions=True)
		emit_audit_event(
			"delegation_created",
			logger_name="medops.delegation",
			policy_digest=doc.policy_digest,
			matrix_revision=int(doc.matrix_revision),
			status=doc.status,
		)
		return doc.as_public_dict()


class RevokeDelegationService(DomainService[DelegationRevoke]):
	definition = DomainServiceDefinition(
		name="hrp_workflow_authorization.delegation.revoke",
		version=1,
		kind="command",
		required_roles=DELEGATION_CREATE_ROLES,
	)

	def authorize(self, command: DelegationRevoke) -> None:
		require_roles(self.definition.required_roles)
		doc = _delegation_doc(command.delegation)
		if (
			not DELEGATION_ADMIN_ROLES.intersection(frappe.get_roles())
			and doc.from_user != frappe.session.user
		):
			raise_ione_error("PERMISSION_DENIED")

	def request_payload(self, command: DelegationRevoke) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: DelegationRevoke) -> None:
		_delegation_doc(command.delegation)

	def perform(self, command: DelegationRevoke) -> dict[str, object]:
		doc = _delegation_doc(command.delegation, lock=True)
		if doc.status not in {"Scheduled", "Active"}:
			raise_ione_error("INVALID_STATE_TRANSITION")
		doc.status = "Revoked"
		doc.revoked_at = now_datetime()
		doc.revoked_by = frappe.session.user
		doc.revocation_reason = command.reason
		doc.flags.delegation_service_write = True
		doc.save(ignore_permissions=True)
		emit_audit_event(
			"delegation_revoked",
			logger_name="medops.delegation",
			policy_digest=doc.policy_digest,
			matrix_revision=int(doc.matrix_revision),
		)
		return doc.as_public_dict()


def create_delegation(
	command: DelegationCreate,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		CreateDelegationService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def revoke_delegation(
	command: DelegationRevoke,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		RevokeDelegationService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def get_delegation(name: str, *, correlation_id: object | None = None) -> dict[str, object]:
	with service_audit_scope(correlation_id) as context:
		require_roles(DELEGATION_READ_ROLES)
		doc = _delegation_doc(name)
		roles = set(frappe.get_roles())
		if not ({*DELEGATION_ADMIN_ROLES, "HRP Auditor"}.intersection(roles)) and frappe.session.user not in {
			doc.from_user,
			doc.to_user,
		}:
			raise_ione_error("PERMISSION_DENIED")
		emit_audit_event(
			"delegation_read",
			logger_name="medops.delegation",
			policy_digest=doc.policy_digest,
			status=doc.status,
		)
		return {
			**doc.as_public_dict(),
			"correlation_id": context.correlation_id,
			"request_id": context.request_id,
			"idempotency_replayed": False,
		}


def apply_active_delegations(
	*,
	matrix: HRPApprovalMatrix,
	target_doc: Document,
	resolved_rows: list[tuple[ApprovalMatrixStep, list[ResolvedApprover]]],
) -> list[tuple[ApprovalMatrixStep, list[ResolvedApprover]]]:
	today = nowdate()
	from medops.common.budget_workflow import CASE

	case_filters = (
		{"from_user": ("in", sorted({entry["user"] for _, entries in resolved_rows for entry in entries}))}
		if target_doc.doctype == CASE
		else {}
	)
	rows = frappe.get_all(
		"HRP Delegation",
		filters={
			"matrix": matrix.name,
			"matrix_revision": int(matrix.revision),
			"matrix_digest": matrix.policy_digest,
			"status": ("in", ["Scheduled", "Active"]),
			"valid_from": ("<=", today),
			"valid_to": (">=", today),
			**case_filters,
		},
		fields=["name", "from_user", "to_user", "step_sequence"],
		order_by="name asc",
		limit=MAX_DELEGATION_CANDIDATES + 1,
	)
	if len(rows) > MAX_DELEGATION_CANDIDATES:
		raise_ione_error("CONFIGURATION_INVALID")
	grants: list[DelegationGrant] = []
	for row in rows:
		delegate = str(row.to_user)
		if not _user_is_active(delegate) or not can_approve_target(delegate, target_doc):
			raise_ione_error("CONFIGURATION_INVALID")
		grants.append(
			DelegationGrant(
				name=str(row.name),
				from_user=str(row.from_user),
				to_user=delegate,
				step_sequence=int(row.step_sequence) if row.step_sequence else None,
			)
		)
	try:
		result = apply_delegation_grants(resolved_rows, tuple(grants))
	except ValueError as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)
	if grants:
		emit_audit_event(
			"delegation_applied",
			logger_name="medops.delegation",
			matrix_revision=int(matrix.revision),
			delegation_count=len(grants),
		)
	return result


def sync_delegation_statuses() -> dict[str, int]:
	if not frappe.db.table_exists("HRP Delegation"):
		return {"activated": 0, "expired": 0}
	today = nowdate()
	activated = 0
	expired = 0
	for row in frappe.get_all(
		"HRP Delegation",
		filters={"status": ("in", ["Scheduled", "Active"])},
		fields=["name", "status", "valid_from", "valid_to"],
		limit=10000,
	):
		new_status: str | None = None
		if str(row.valid_to) < today:
			new_status = "Expired"
			expired += 1
		elif row.status == "Scheduled" and str(row.valid_from) <= today:
			new_status = "Active"
			activated += 1
		if new_status is None:
			continue
		doc = _delegation_doc(str(row.name), lock=True)
		doc.status = new_status
		if new_status == "Expired":
			doc.expired_at = now_datetime()
		doc.flags.delegation_service_write = True
		doc.save(ignore_permissions=True)
	if activated or expired:
		emit_audit_event(
			"delegation_statuses_synchronized",
			logger_name="medops.delegation",
			activated=activated,
			expired=expired,
		)
	return {"activated": activated, "expired": expired}


__all__ = [
	"DELEGATION_ADMIN_ROLES",
	"DELEGATION_CREATE_ROLES",
	"DELEGATION_READ_ROLES",
	"CreateDelegationService",
	"RevokeDelegationService",
	"apply_active_delegations",
	"create_delegation",
	"get_delegation",
	"revoke_delegation",
	"sync_delegation_statuses",
]
