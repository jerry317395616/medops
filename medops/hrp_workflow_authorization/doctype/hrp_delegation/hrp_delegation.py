from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from frappe.model.document import Document

from medops.common.delegation import (
	DelegationContractError,
	DelegationDefinition,
	build_delegation_definition,
)
from medops.services.audit_context import emit_audit_event
from medops.services.errors import raise_ione_error


class HRPDelegation(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		company: DF.Link
		expired_at: DF.Datetime | None
		from_user: DF.Link
		hospital: DF.Link
		include_descendants: DF.Check
		matrix: DF.Link
		matrix_digest: DF.Data
		matrix_revision: DF.Int
		organization_unit: DF.Link | None
		policy_digest: DF.Data
		reason: DF.SmallText | None
		revocation_reason: DF.SmallText | None
		revoked_at: DF.Datetime | None
		revoked_by: DF.Link | None
		status: Literal["Scheduled", "Active", "Revoked", "Expired"]
		step_sequence: DF.Int | None
		target_doctype: DF.Link
		to_user: DF.Link
		valid_from: DF.Date
		valid_to: DF.Date
	# end: auto-generated types

	def before_insert(self) -> None:
		self._require_service_write()

	def before_save(self) -> None:
		self._require_service_write()

	def validate(self) -> None:
		definition = self.as_definition()
		for fieldname, value in (
			("matrix", definition.matrix),
			("matrix_revision", definition.matrix_revision),
			("matrix_digest", definition.matrix_digest),
			("target_doctype", definition.target_doctype),
			("company", definition.company),
			("hospital", definition.hospital),
			("organization_unit", definition.organization_unit),
			("include_descendants", int(definition.include_descendants)),
			("from_user", definition.from_user),
			("to_user", definition.to_user),
			("valid_from", definition.valid_from),
			("valid_to", definition.valid_to),
			("step_sequence", definition.step_sequence or 0),
			("reason", definition.reason),
			("status", definition.status),
			("policy_digest", definition.policy_digest),
		):
			self.set(fieldname, value)
		self._validate_immutable_policy()

	def on_update(self) -> None:
		emit_audit_event(
			"delegation_saved",
			logger_name="medops.delegation",
			policy_digest=self.policy_digest,
			matrix_revision=int(self.matrix_revision),
			status=self.status,
		)

	def on_trash(self) -> None:
		if not getattr(self.flags, "delegation_migration", False):
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def as_definition(self) -> DelegationDefinition:
		try:
			return build_delegation_definition(
				matrix=self.matrix,
				matrix_revision=self.matrix_revision,
				matrix_digest=self.matrix_digest,
				target_doctype=self.target_doctype,
				company=self.company,
				hospital=self.hospital,
				organization_unit=self.organization_unit,
				include_descendants=self.include_descendants,
				from_user=self.from_user,
				to_user=self.to_user,
				valid_from=self.valid_from,
				valid_to=self.valid_to,
				step_sequence=self.step_sequence or None,
				reason=self.reason,
				status=self.status,
			)
		except DelegationContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)

	def as_public_dict(self) -> dict[str, object]:
		return {
			"name": self.name,
			**self.as_definition().as_public_dict(),
			"revoked_at": str(self.revoked_at) if self.revoked_at else None,
			"revoked_by": self.revoked_by or None,
			"revocation_reason": self.revocation_reason or None,
			"expired_at": str(self.expired_at) if self.expired_at else None,
		}

	def _require_service_write(self) -> None:
		if not getattr(self.flags, "delegation_service_write", False):
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def _validate_immutable_policy(self) -> None:
		before = self.get_doc_before_save()
		if before is None:
			return
		if before.policy_digest != self.policy_digest:
			raise_ione_error("OPERATION_NOT_ALLOWED")


__all__ = ["HRPDelegation"]
