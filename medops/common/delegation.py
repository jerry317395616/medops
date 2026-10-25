from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from medops.common.approval_matrix import ApprovalMatrixStep, ResolvedApprover
from medops.common.domain_service import fingerprint_json
from medops.common.organization import (
	OrganizationContractError,
	normalize_optional_text,
	normalize_positive_integer,
	normalize_reference,
	normalize_required_date,
	validate_date_range,
)

DELEGATION_SCHEMA_VERSION = 1
DELEGATION_DOCTYPE = "HRP Delegation"
DELEGATION_STATUSES = ("Scheduled", "Active", "Revoked", "Expired")
MAX_DELEGATION_DAYS = 366
DelegationStatus = Literal["Scheduled", "Active", "Revoked", "Expired"]


class DelegationContractError(ValueError):
	"""Raised when an approval delegation violates the public contract."""


@dataclass(frozen=True, slots=True)
class DelegationCreate:
	matrix: str
	from_user: str
	to_user: str
	valid_from: str
	valid_to: str
	step_sequence: int | None
	reason: str | None

	def as_request_payload(self) -> dict[str, object]:
		return {
			"matrix": self.matrix,
			"from_user": self.from_user,
			"to_user": self.to_user,
			"valid_from": self.valid_from,
			"valid_to": self.valid_to,
			"step_sequence": self.step_sequence,
			"reason": self.reason,
		}


@dataclass(frozen=True, slots=True)
class DelegationDefinition:
	matrix: str
	matrix_revision: int
	matrix_digest: str
	target_doctype: str
	company: str
	hospital: str
	organization_unit: str | None
	include_descendants: bool
	from_user: str
	to_user: str
	valid_from: str
	valid_to: str
	step_sequence: int | None
	reason: str | None
	status: DelegationStatus
	policy_digest: str

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": DELEGATION_SCHEMA_VERSION,
			"matrix": self.matrix,
			"matrix_revision": self.matrix_revision,
			"matrix_digest": self.matrix_digest,
			"target_doctype": self.target_doctype,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit,
			"include_descendants": self.include_descendants,
			"from_user": self.from_user,
			"to_user": self.to_user,
			"valid_from": self.valid_from,
			"valid_to": self.valid_to,
			"step_sequence": self.step_sequence,
			"reason": self.reason,
			"status": self.status,
			"policy_digest": self.policy_digest,
		}


@dataclass(frozen=True, slots=True)
class DelegationRevoke:
	delegation: str
	reason: str

	def as_request_payload(self) -> dict[str, object]:
		return {"delegation": self.delegation, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class DelegationGrant:
	name: str
	from_user: str
	to_user: str
	step_sequence: int | None


def build_delegation_create(
	*,
	matrix: object,
	from_user: object,
	to_user: object,
	valid_from: object,
	valid_to: object,
	step_sequence: object = None,
	reason: object = None,
) -> DelegationCreate:
	try:
		start = normalize_required_date(valid_from, label="valid_from")
		end = normalize_required_date(valid_to, label="valid_to")
		validate_date_range(start, end)
		if (date.fromisoformat(end) - date.fromisoformat(start)).days + 1 > MAX_DELEGATION_DAYS:
			raise DelegationContractError("delegation validity exceeds the maximum duration")
		delegator = normalize_reference(from_user, label="from_user")
		delegate = normalize_reference(to_user, label="to_user")
		if delegator == delegate:
			raise DelegationContractError("from_user and to_user must be different")
		sequence = (
			normalize_positive_integer(step_sequence, label="step_sequence")
			if step_sequence not in (None, "")
			else None
		)
		return DelegationCreate(
			matrix=normalize_reference(matrix, label="matrix"),
			from_user=delegator,
			to_user=delegate,
			valid_from=start,
			valid_to=end,
			step_sequence=sequence,
			reason=normalize_optional_text(reason, label="reason"),
		)
	except OrganizationContractError as exc:
		raise DelegationContractError(str(exc)) from exc


def build_delegation_definition(
	*,
	matrix: object,
	matrix_revision: object,
	matrix_digest: object,
	target_doctype: object,
	company: object,
	hospital: object,
	organization_unit: object,
	include_descendants: object,
	from_user: object,
	to_user: object,
	valid_from: object,
	valid_to: object,
	step_sequence: object = None,
	reason: object = None,
	status: object,
) -> DelegationDefinition:
	command = build_delegation_create(
		matrix=matrix,
		from_user=from_user,
		to_user=to_user,
		valid_from=valid_from,
		valid_to=valid_to,
		step_sequence=step_sequence,
		reason=reason,
	)
	try:
		revision = normalize_positive_integer(matrix_revision, label="matrix_revision")
		digest = normalize_reference(matrix_digest, label="matrix_digest")
		if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
			raise DelegationContractError("matrix_digest is invalid")
		if status not in DELEGATION_STATUSES:
			raise DelegationContractError("status is invalid")
		unit = (
			normalize_reference(organization_unit, label="organization_unit")
			if organization_unit not in (None, "")
			else None
		)
		if include_descendants not in (False, True, 0, 1, "0", "1"):
			raise DelegationContractError("include_descendants must be boolean")
		descendants = include_descendants in (True, 1, "1")
		if descendants and unit is None:
			raise DelegationContractError("include_descendants requires organization_unit")
		payload = {
			"matrix": command.matrix,
			"matrix_revision": revision,
			"matrix_digest": digest,
			"target_doctype": normalize_reference(target_doctype, label="target_doctype"),
			"company": normalize_reference(company, label="company"),
			"hospital": normalize_reference(hospital, label="hospital"),
			"organization_unit": unit,
			"include_descendants": descendants,
			"from_user": command.from_user,
			"to_user": command.to_user,
			"valid_from": command.valid_from,
			"valid_to": command.valid_to,
			"step_sequence": command.step_sequence,
			"reason": command.reason,
		}
	except OrganizationContractError as exc:
		raise DelegationContractError(str(exc)) from exc
	if not isinstance(status, str):
		raise DelegationContractError("status is invalid")
	return DelegationDefinition(
		matrix=str(payload["matrix"]),
		matrix_revision=int(payload["matrix_revision"]),
		matrix_digest=str(payload["matrix_digest"]),
		target_doctype=str(payload["target_doctype"]),
		company=str(payload["company"]),
		hospital=str(payload["hospital"]),
		organization_unit=unit,
		include_descendants=descendants,
		from_user=str(payload["from_user"]),
		to_user=str(payload["to_user"]),
		valid_from=str(payload["valid_from"]),
		valid_to=str(payload["valid_to"]),
		step_sequence=command.step_sequence,
		reason=command.reason,
		status=status,
		policy_digest=fingerprint_json({"schema_version": DELEGATION_SCHEMA_VERSION, **payload}),
	)


def build_delegation_revoke(*, delegation: object, reason: object) -> DelegationRevoke:
	try:
		normalized_reason = normalize_optional_text(reason, label="reason")
		if normalized_reason is None:
			raise DelegationContractError("reason is required")
		return DelegationRevoke(
			delegation=normalize_reference(delegation, label="delegation"),
			reason=normalized_reason,
		)
	except OrganizationContractError as exc:
		raise DelegationContractError(str(exc)) from exc


def apply_delegation_grants(
	resolved_rows: list[tuple[ApprovalMatrixStep, list[ResolvedApprover]]],
	grants: tuple[DelegationGrant, ...],
) -> list[tuple[ApprovalMatrixStep, list[ResolvedApprover]]]:
	result: list[tuple[ApprovalMatrixStep, list[ResolvedApprover]]] = []
	for step, approvers in resolved_rows:
		delegated: list[ResolvedApprover] = []
		for approver in approvers:
			matches = [
				grant
				for grant in grants
				if grant.from_user == approver["user"] and grant.step_sequence in (None, step.sequence_no)
			]
			if len(matches) > 1:
				raise DelegationContractError("multiple active delegations match one approval responsibility")
			if not matches:
				delegated.append(approver)
				continue
			grant = matches[0]
			delegated.append(
				{
					**approver,
					"user": grant.to_user,
					"delegated_from": grant.from_user,
					"delegation": grant.name,
				}
			)
		result.append((step, delegated))
	return result


__all__ = [
	"DELEGATION_DOCTYPE",
	"DELEGATION_SCHEMA_VERSION",
	"DELEGATION_STATUSES",
	"MAX_DELEGATION_DAYS",
	"DelegationContractError",
	"DelegationCreate",
	"DelegationDefinition",
	"DelegationGrant",
	"DelegationRevoke",
	"apply_delegation_grants",
	"build_delegation_create",
	"build_delegation_definition",
	"build_delegation_revoke",
]
