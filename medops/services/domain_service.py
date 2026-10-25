from __future__ import annotations

from abc import ABC, abstractmethod
from hashlib import sha256
from typing import Generic, TypeVar

import frappe

from medops.common.domain_service import (
	DomainServiceContractError,
	DomainServiceDefinition,
	DomainServiceExecution,
	canonical_json_object,
)
from medops.common.error_catalog import IoneApplicationError
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.errors import raise_ione_error, require_roles
from medops.services.idempotency import (
	complete_idempotency,
	reserve_idempotency,
	resolve_idempotency_key,
)

CommandT = TypeVar("CommandT")


class DomainService(ABC, Generic[CommandT]):
	"""Template for permissioned, audited and idempotent domain operations."""

	definition: DomainServiceDefinition

	def authorize(self, command: CommandT) -> None:
		del command
		require_roles(self.definition.required_roles)

	def validate(self, command: CommandT) -> None:
		del command

	@abstractmethod
	def request_payload(self, command: CommandT) -> dict[str, object]:
		"""Return the deterministic request used only to calculate a fingerprint."""

	@abstractmethod
	def perform(self, command: CommandT) -> dict[str, object]:
		"""Execute domain logic without committing the Frappe transaction."""

	def execute(
		self,
		command: CommandT,
		*,
		idempotency_key: object | None = None,
		correlation_id: object | None = None,
	) -> DomainServiceExecution:
		with service_audit_scope(correlation_id) as context:
			savepoint: str | None = None
			emit_audit_event(
				"domain_service_started",
				logger_name="medops.domain_service",
				service_name=self.definition.name,
				service_version=self.definition.version,
				service_kind=self.definition.kind,
			)
			try:
				self.authorize(command)
				self.validate(command)
				request_payload, _ = canonical_json_object(self.request_payload(command))
				reservation = None
				if self.definition.kind == "command":
					resolved_key = resolve_idempotency_key(idempotency_key)
					savepoint = self._savepoint_name(context.request_id)
					frappe.db.savepoint(savepoint)
					reservation = reserve_idempotency(
						definition=self.definition,
						idempotency_key=resolved_key,
						request_payload=request_payload,
						context=context,
					)
					if reservation.replayed:
						emit_audit_event(
							"domain_service_replayed",
							logger_name="medops.domain_service",
							service_name=self.definition.name,
							service_version=self.definition.version,
						)
						return DomainServiceExecution(
							service=self.definition.name,
							service_version=self.definition.version,
							result=dict(reservation.replayed_result or {}),
							correlation_id=context.correlation_id,
							request_id=context.request_id,
							idempotency_replayed=True,
						)

				result, _ = canonical_json_object(self.perform(command))
				if reservation is not None:
					complete_idempotency(
						reservation,
						result=result,
						context=context,
					)
				emit_audit_event(
					"domain_service_completed",
					logger_name="medops.domain_service",
					service_name=self.definition.name,
					service_version=self.definition.version,
					service_kind=self.definition.kind,
				)
				return DomainServiceExecution(
					service=self.definition.name,
					service_version=self.definition.version,
					result=result,
					correlation_id=context.correlation_id,
					request_id=context.request_id,
					idempotency_replayed=False,
				)
			except IoneApplicationError as exc:
				self._rollback(savepoint)
				self._audit_failure(error_code=exc.code)
				raise
			except DomainServiceContractError as exc:
				self._rollback(savepoint)
				self._audit_failure(cause_type=type(exc).__name__)
				raise_ione_error("INVALID_REQUEST", cause=exc)
			except Exception as exc:
				self._rollback(savepoint)
				self._audit_failure(cause_type=type(exc).__name__)
				raise_ione_error("INTERNAL_ERROR", cause=exc)

	@staticmethod
	def _savepoint_name(request_id: str) -> str:
		digest = sha256(request_id.encode()).hexdigest()[:20]
		return f"ione_service_{digest}"

	@staticmethod
	def _rollback(savepoint: str | None) -> None:
		if savepoint is not None:
			frappe.db.rollback(save_point=savepoint)

	def _audit_failure(
		self,
		*,
		error_code: str | None = None,
		cause_type: str | None = None,
	) -> None:
		fields: dict[str, object] = {
			"service_name": self.definition.name,
			"service_version": self.definition.version,
			"service_kind": self.definition.kind,
		}
		if error_code is not None:
			fields["error_code"] = error_code
		if cause_type is not None:
			fields["cause_type"] = cause_type
		emit_audit_event(
			"domain_service_failed",
			level="warning",
			logger_name="medops.domain_service",
			**fields,
		)


__all__ = ["DomainService"]
