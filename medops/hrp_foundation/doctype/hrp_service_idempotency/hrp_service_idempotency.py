from __future__ import annotations

from typing import TYPE_CHECKING

from frappe.model.document import Document

from medops.common.audit_context import normalize_correlation_id, normalize_request_id
from medops.common.domain_service import (
	IDEMPOTENCY_RESPONSE_SCHEMA_VERSION,
	normalize_service_name,
	normalize_sha256,
)
from medops.services.errors import raise_ione_error

if TYPE_CHECKING:
	from frappe.types import DF


class HRPServiceIdempotency(Document):
	if TYPE_CHECKING:
		completed_at: DF.Datetime | None
		correlation_id: DF.Data
		expires_at: DF.Datetime
		idempotency_key_hash: DF.Data
		request_fingerprint: DF.Data
		request_id: DF.Data
		response_fingerprint: DF.Data | None
		response_schema_version: DF.Int
		response_snapshot: DF.LongText | None
		service_name: DF.Data
		service_version: DF.Int
		status: DF.Data

	def validate(self) -> None:
		try:
			normalize_service_name(self.service_name)
			normalize_sha256(self.idempotency_key_hash, label="idempotency_key_hash")
			normalize_sha256(self.request_fingerprint, label="request_fingerprint")
			normalize_correlation_id(self.correlation_id)
			normalize_request_id(self.request_id)
			if not isinstance(self.service_version, int) or not 1 <= self.service_version <= 999:
				raise ValueError("service_version is invalid")
			if self.response_schema_version != IDEMPOTENCY_RESPONSE_SCHEMA_VERSION:
				raise ValueError("response_schema_version is invalid")
			if self.status not in {"In Progress", "Completed"}:
				raise ValueError("status is invalid")
			if self.status == "Completed":
				normalize_sha256(self.response_fingerprint, label="response_fingerprint")
				if not self.response_snapshot or not self.completed_at:
					raise ValueError("completed record is incomplete")
			elif self.response_snapshot or self.response_fingerprint or self.completed_at:
				raise ValueError("in-progress record contains a response")
		except ValueError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)

		previous = self.get_doc_before_save()
		if previous is None:
			return
		for fieldname in (
			"service_name",
			"service_version",
			"idempotency_key_hash",
			"request_fingerprint",
			"correlation_id",
			"request_id",
			"expires_at",
		):
			if self.get(fieldname) != previous.get(fieldname):
				raise_ione_error("OPERATION_NOT_ALLOWED")
		if previous.status == "Completed":
			raise_ione_error("OPERATION_NOT_ALLOWED")
