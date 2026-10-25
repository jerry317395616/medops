from __future__ import annotations

import json
from typing import TYPE_CHECKING

from frappe.model.document import Document

from medops.common.domain_service import (
	DomainServiceContractError,
	canonical_json,
	normalize_sha256,
)
from medops.common.numbering import (
	NumberingContractError,
	dimensions_digest_for,
	normalize_dimensions,
	normalize_reservation_token,
)
from medops.common.organization import (
	OrganizationContractError,
	normalize_code,
	normalize_positive_integer,
	normalize_reference,
	normalize_required_date,
	normalize_required_text,
)
from medops.services.errors import raise_ione_error


class HRPNumberReservation(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		allocated_by: DF.Link
		business_date: DF.Date
		company: DF.Link
		correlation_id: DF.Data
		counter_key: DF.Data
		dimensions_digest: DF.Data
		dimensions_json: DF.Code
		hospital: DF.Link
		number: DF.Data
		numbering_scheme: DF.Link
		organization_unit: DF.Link | None
		request_id: DF.Data
		reservation_token: DF.Data
		reset_bucket: DF.Data
		scheme_code: DF.Data
		scheme_revision: DF.Int
		sequence_value: DF.Int
		template_digest: DF.Data
	# end: auto-generated types

	def before_insert(self) -> None:
		self._require_service_write()

	def validate(self) -> None:
		self._require_service_write()
		if not self.is_new():
			raise_ione_error("OPERATION_NOT_ALLOWED")
		try:
			self.numbering_scheme = normalize_reference(
				self.numbering_scheme,
				label="numbering_scheme",
			)
			self.scheme_code = normalize_code(self.scheme_code, label="scheme_code")
			self.number = normalize_required_text(self.number, label="number")
			self.reservation_token = normalize_reservation_token(self.reservation_token)
			self.business_date = normalize_required_date(self.business_date, label="business_date")
			self.company = normalize_reference(self.company, label="company")
			self.hospital = normalize_code(self.hospital, label="hospital")
			self.organization_unit = (
				normalize_reference(self.organization_unit, label="organization_unit")
				if self.organization_unit
				else None
			)
			dimensions = normalize_dimensions(json.loads(self.dimensions_json or "{}"))
			self.dimensions_json = canonical_json(dimensions)
			self.dimensions_digest = normalize_sha256(
				self.dimensions_digest,
				label="dimensions_digest",
			)
			self.reset_bucket = normalize_required_text(
				self.reset_bucket,
				label="reset_bucket",
				maximum=16,
			)
			self.counter_key = normalize_required_text(
				self.counter_key,
				label="counter_key",
				maximum=140,
			)
			self.sequence_value = normalize_positive_integer(
				self.sequence_value,
				label="sequence_value",
			)
			self.scheme_revision = normalize_positive_integer(
				self.scheme_revision,
				label="scheme_revision",
			)
			self.template_digest = normalize_sha256(
				self.template_digest,
				label="template_digest",
			)
			self.allocated_by = normalize_reference(self.allocated_by, label="allocated_by")
			self.correlation_id = normalize_reference(self.correlation_id, label="correlation_id")
			self.request_id = normalize_reference(self.request_id, label="request_id")
		except (
			json.JSONDecodeError,
			DomainServiceContractError,
			NumberingContractError,
			OrganizationContractError,
		) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)

		if self.dimensions_digest != dimensions_digest_for(dimensions):
			raise_ione_error("CONFIGURATION_INVALID")

	def before_save(self) -> None:
		self._require_service_write()
		if not self.is_new():
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def on_trash(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def _require_service_write(self) -> None:
		if not (
			getattr(self.flags, "numbering_service_write", False)
			or getattr(self.flags, "numbering_migration", False)
		):
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def get_dimensions(self) -> dict[str, str]:
		try:
			loaded = json.loads(self.dimensions_json or "{}")
		except json.JSONDecodeError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		return normalize_dimensions(loaded)

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": 1,
			"number": self.number,
			"reservation_token": self.reservation_token,
			"scheme": self.scheme_code,
			"date": str(self.business_date),
			"dimensions": self.get_dimensions(),
		}


__all__ = ["HRPNumberReservation"]
