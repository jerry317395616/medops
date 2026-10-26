from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime
from frappe.utils.password import decrypt, encrypt

from medops.common.audit_context import AuditContext
from medops.common.domain_service import (
	IDEMPOTENCY_RESPONSE_SCHEMA_VERSION,
	DomainServiceContractError,
	DomainServiceDefinition,
	canonical_json_object,
	fingerprint_json,
	idempotency_key_hash,
	idempotency_record_name,
	normalize_idempotency_key,
	normalize_sha256,
)
from medops.services.errors import raise_ione_error, require_roles

if TYPE_CHECKING:
	from medops.hrp_foundation.doctype.hrp_service_idempotency.hrp_service_idempotency import (
		HRPServiceIdempotency,
	)

IDEMPOTENCY_DOCTYPE = "HRP Service Idempotency"
STATUS_IN_PROGRESS = "In Progress"
STATUS_COMPLETED = "Completed"


@dataclass(frozen=True, slots=True)
class IdempotencyReservation:
	record_name: str
	request_fingerprint: str
	replayed_result: dict[str, object] | None = None

	@property
	def replayed(self) -> bool:
		return self.replayed_result is not None


def resolve_idempotency_key(explicit: object | None = None) -> str:
	candidate = explicit
	if candidate in (None, ""):
		request = getattr(frappe.local, "request", None)
		if request is not None:
			candidate = request.headers.get("Idempotency-Key")
	try:
		return normalize_idempotency_key(candidate)
	except DomainServiceContractError as exc:
		raise_ione_error("INVALID_REQUEST", cause=exc)


def _load_record(record_name: str) -> HRPServiceIdempotency | None:
	if not frappe.db.exists(IDEMPOTENCY_DOCTYPE, record_name):
		return None
	return cast("HRPServiceIdempotency", frappe.get_doc(IDEMPOTENCY_DOCTYPE, record_name))


def _delete_if_expired(record: HRPServiceIdempotency) -> bool:
	try:
		expires_at = get_datetime(record.expires_at)
	except (TypeError, ValueError):
		raise_ione_error(
			"CONFIGURATION_INVALID",
			cause=DomainServiceContractError("idempotency expiration is invalid"),
		)
	if expires_at > now_datetime():
		return False
	frappe.delete_doc(
		IDEMPOTENCY_DOCTYPE,
		record.name,
		ignore_permissions=True,
		force=True,
	)
	return True


def _assert_record_matches(
	record: HRPServiceIdempotency,
	*,
	definition: DomainServiceDefinition,
	key_hash: str,
	request_fingerprint: str,
) -> None:
	try:
		service_version = int(record.service_version)
	except (TypeError, ValueError):
		raise_ione_error(
			"CONFIGURATION_INVALID",
			cause=DomainServiceContractError("idempotency service version is invalid"),
		)
	if (
		record.owner != frappe.session.user
		or record.service_name != definition.name
		or service_version != definition.version
		or record.idempotency_key_hash != key_hash
		or record.request_fingerprint != request_fingerprint
	):
		raise_ione_error("IDEMPOTENCY_CONFLICT")


def _load_replayed_result(record: HRPServiceIdempotency) -> dict[str, object]:
	if record.status != STATUS_COMPLETED:
		raise_ione_error("CONFLICT")
	if int(record.response_schema_version) != IDEMPOTENCY_RESPONSE_SCHEMA_VERSION:
		raise_ione_error("CONFIGURATION_INVALID")
	try:
		serialized = decrypt(record.response_snapshot)
		normalize_sha256(record.response_fingerprint, label="response_fingerprint")
		if fingerprint_json(json.loads(serialized)) != record.response_fingerprint:
			raise DomainServiceContractError("idempotency response fingerprint mismatch")
		result, canonical = canonical_json_object(json.loads(serialized))
		if canonical != serialized:
			raise DomainServiceContractError("idempotency response is not canonical")
		return result
	except Exception as exc:
		safe_cause = (
			exc
			if isinstance(exc, DomainServiceContractError)
			else DomainServiceContractError("idempotency response snapshot is invalid")
		)
		raise_ione_error("CONFIGURATION_INVALID", cause=safe_cause)


def reserve_idempotency(
	*,
	definition: DomainServiceDefinition,
	idempotency_key: str,
	request_payload: dict[str, object],
	context: AuditContext,
) -> IdempotencyReservation:
	record_name = idempotency_record_name(definition.name, idempotency_key)
	key_hash = idempotency_key_hash(idempotency_key)
	request_fingerprint = fingerprint_json(
		{
			"schema_version": 1,
			"service": definition.name,
			"service_version": definition.version,
			"request": request_payload,
		}
	)
	existing = _load_record(record_name)
	if existing is not None and _delete_if_expired(existing):
		existing = None
	if existing is not None:
		_assert_record_matches(
			existing,
			definition=definition,
			key_hash=key_hash,
			request_fingerprint=request_fingerprint,
		)
		return IdempotencyReservation(
			record_name=record_name,
			request_fingerprint=request_fingerprint,
			replayed_result=_load_replayed_result(existing),
		)

	candidate = frappe.get_doc(
		{
			"doctype": IDEMPOTENCY_DOCTYPE,
			"name": record_name,
			"service_name": definition.name,
			"service_version": definition.version,
			"idempotency_key_hash": key_hash,
			"request_fingerprint": request_fingerprint,
			"status": STATUS_IN_PROGRESS,
			"correlation_id": context.correlation_id,
			"request_id": context.request_id,
			"response_schema_version": IDEMPOTENCY_RESPONSE_SCHEMA_VERSION,
			"expires_at": add_to_date(
				now_datetime(),
				seconds=definition.idempotency_ttl_seconds,
			),
		}
	)
	candidate.insert(ignore_permissions=True, ignore_if_duplicate=True)
	stored = _load_record(record_name)
	if stored is None:
		raise_ione_error("INTERNAL_ERROR")
	_assert_record_matches(
		stored,
		definition=definition,
		key_hash=key_hash,
		request_fingerprint=request_fingerprint,
	)
	if stored.request_id != context.request_id:
		return IdempotencyReservation(
			record_name=record_name,
			request_fingerprint=request_fingerprint,
			replayed_result=_load_replayed_result(stored),
		)
	if stored.status != STATUS_IN_PROGRESS:
		raise_ione_error("CONFIGURATION_INVALID")
	return IdempotencyReservation(
		record_name=record_name,
		request_fingerprint=request_fingerprint,
	)


def complete_idempotency(
	reservation: IdempotencyReservation,
	*,
	result: dict[str, object],
	context: AuditContext,
) -> None:
	record = _load_record(reservation.record_name)
	if (
		record is None
		or record.status != STATUS_IN_PROGRESS
		or record.request_id != context.request_id
		or record.request_fingerprint != reservation.request_fingerprint
	):
		raise_ione_error("CONFLICT")
	normalized_result, serialized = canonical_json_object(result)
	record.response_snapshot = encrypt(serialized)
	record.response_fingerprint = fingerprint_json(normalized_result)
	record.response_schema_version = IDEMPOTENCY_RESPONSE_SCHEMA_VERSION
	record.status = STATUS_COMPLETED
	record.completed_at = now_datetime()
	record.save(ignore_permissions=True)


def read_owned_idempotency_receipt(
	definition: DomainServiceDefinition, idempotency_key: str, request_payload: dict[str, object]
) -> dict[str, object]:
	"""Internal adapter only: decrypt an existing owned receipt without renewing it.

	Unlike execute/reserve this never creates, deletes, expires or reclaims work.
	Callers must enforce the business authority and never expose secret results.
	"""
	require_roles(definition.required_roles)
	key = normalize_idempotency_key(idempotency_key)
	record = _load_record(idempotency_record_name(definition.name, key))
	if record is None:
		raise_ione_error("RESOURCE_NOT_FOUND")
	payload, _ = canonical_json_object(request_payload)
	_assert_record_matches(
		record,
		definition=definition,
		key_hash=idempotency_key_hash(key),
		request_fingerprint=fingerprint_json(
			dict(
				schema_version=1, service=definition.name, service_version=definition.version, request=payload
			)
		),
	)
	if get_datetime(record.expires_at) <= now_datetime():
		raise_ione_error("CONFLICT")
	return _load_replayed_result(record)


__all__ = [
	"IDEMPOTENCY_DOCTYPE",
	"IdempotencyReservation",
	"complete_idempotency",
	"read_owned_idempotency_receipt",
	"reserve_idempotency",
	"resolve_idempotency_key",
]
