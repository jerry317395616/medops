from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from types import MappingProxyType
from typing import Literal, TypedDict

from medops.common.domain_service import (
	DomainServiceContractError,
	canonical_json_object,
	normalize_sha256,
)
from medops.common.private_values import PrivateText

MESSAGE_BOX_SCHEMA_VERSION = 1
MAX_EVENT_ID_LENGTH = 140
MAX_EVENT_TYPE_LENGTH = 140
MAX_ENDPOINT_LENGTH = 140
MAX_PROCESSING_TOKEN_LENGTH = 256
MAX_ERROR_CODE_LENGTH = 80
MAX_ERROR_MESSAGE_LENGTH = 500

EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,139}$")
EVENT_TYPE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{2,139}$")
ENDPOINT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,139}$")
PROCESSING_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_.:-]{1,79}$")

MessageBoxKind = Literal["outbox", "inbox"]
OUTBOX_STATUSES = ("Pending", "Processing", "Delivered", "Failed", "Dead Letter")
INBOX_STATUSES = ("Processing", "Processed", "Failed", "Ignored")
MESSAGE_STATUSES = MappingProxyType(
	{
		"outbox": OUTBOX_STATUSES,
		"inbox": INBOX_STATUSES,
	}
)


class TransactionalMessageContractError(DomainServiceContractError):
	"""Raised when a transactional message violates the shared wire contract."""


@dataclass(frozen=True, slots=True)
class MessageField:
	fieldname: str
	fieldtype: str
	required: bool = False
	hidden: bool = False
	search_index: bool = False

	def as_public_dict(self) -> dict[str, object]:
		return {
			"fieldname": self.fieldname,
			"fieldtype": self.fieldtype,
			"required": self.required,
		}


BASE_MESSAGE_FIELDS = (
	MessageField("event_id", "Data", required=True, search_index=True),
	MessageField("event_type", "Data", required=True, search_index=True),
	MessageField("source", "Data", required=True, search_index=True),
	MessageField("destination", "Data", required=True, search_index=True),
	MessageField("occurred_at", "Datetime", required=True, search_index=True),
	MessageField("payload_schema_version", "Int", required=True),
	MessageField("payload_json", "Code", required=True),
	MessageField("payload_hash", "Data", required=True, hidden=True),
	MessageField("correlation_id", "Data", required=True, search_index=True),
	MessageField("causation_id", "Data", search_index=True),
	MessageField("reference_doctype", "Link"),
	MessageField("reference_name", "Dynamic Link", search_index=True),
	MessageField("status", "Select", required=True, search_index=True),
	MessageField("attempt_count", "Int", required=True),
	MessageField("available_at", "Datetime", required=True, search_index=True),
	MessageField("claim_owner", "Data", hidden=True),
	MessageField("processing_token_hash", "Data", hidden=True),
	MessageField("lease_expires_at", "Datetime", search_index=True),
	MessageField("completed_at", "Datetime", search_index=True),
	MessageField("result_json", "Code", hidden=True),
	MessageField("result_hash", "Data", hidden=True),
	MessageField("last_error_code", "Data"),
	MessageField("last_error_message", "Small Text"),
)
BASE_MESSAGE_FIELDNAMES = frozenset(field.fieldname for field in BASE_MESSAGE_FIELDS)
IMMUTABLE_MESSAGE_FIELDS = frozenset(
	{
		"event_id",
		"event_type",
		"source",
		"destination",
		"occurred_at",
		"payload_schema_version",
		"payload_json",
		"payload_hash",
		"correlation_id",
		"causation_id",
		"reference_doctype",
		"reference_name",
	}
)


@dataclass(frozen=True, slots=True)
class MessageBoxDefinition:
	doctype: str
	kind: MessageBoxKind
	required_roles: frozenset[str]
	max_attempts: int = 10
	default_lease_seconds: int = 300

	def __post_init__(self) -> None:
		if (
			not isinstance(self.doctype, str)
			or not self.doctype.startswith("HRP ")
			or not 5 <= len(self.doctype) <= 140
			or any(ord(character) < 32 for character in self.doctype)
		):
			raise TransactionalMessageContractError("message box DocType is invalid")
		if self.kind not in MESSAGE_STATUSES:
			raise TransactionalMessageContractError("message box kind is invalid")
		if (
			not isinstance(self.required_roles, frozenset)
			or not self.required_roles
			or any(
				not isinstance(role, str)
				or not role
				or role != role.strip()
				or len(role) > 140
				or any(ord(character) < 32 for character in role)
				for role in self.required_roles
			)
		):
			raise TransactionalMessageContractError("message box required_roles is invalid")
		if (
			not isinstance(self.max_attempts, int)
			or isinstance(self.max_attempts, bool)
			or not 1 <= self.max_attempts <= 100
		):
			raise TransactionalMessageContractError("message box max_attempts is invalid")
		if (
			not isinstance(self.default_lease_seconds, int)
			or isinstance(self.default_lease_seconds, bool)
			or not 30 <= self.default_lease_seconds <= 3600
		):
			raise TransactionalMessageContractError("message box lease is invalid")

	@property
	def statuses(self) -> tuple[str, ...]:
		return MESSAGE_STATUSES[self.kind]

	def as_public_dict(self) -> dict[str, object]:
		return {
			"kind": self.kind,
			"max_attempts": self.max_attempts,
			"default_lease_seconds": self.default_lease_seconds,
			"statuses": list(self.statuses),
		}


class MessageSnapshot(TypedDict):
	event_id: str
	event_type: str
	source: str
	destination: str
	occurred_at: str
	payload_schema_version: int
	payload_json: str
	payload_hash: str
	correlation_id: str
	causation_id: str | None
	reference_doctype: str | None
	reference_name: str | None


class TransactionalMessagePublicContract(TypedDict):
	schema_version: int
	base_fields: list[dict[str, object]]
	statuses: dict[str, list[str]]
	delivery_semantics: dict[str, object]
	mutation_policy: dict[str, bool]
	http_write_enabled: bool


def _normalize_pattern(
	value: object,
	*,
	label: str,
	pattern: re.Pattern[str],
	max_length: int,
) -> str:
	if (
		not isinstance(value, str)
		or value != value.strip()
		or len(value) > max_length
		or pattern.fullmatch(value) is None
	):
		raise TransactionalMessageContractError(f"{label} is invalid")
	return value


def normalize_event_id(value: object) -> str:
	return _normalize_pattern(
		value,
		label="event_id",
		pattern=EVENT_ID_PATTERN,
		max_length=MAX_EVENT_ID_LENGTH,
	)


def normalize_event_type(value: object) -> str:
	return _normalize_pattern(
		value,
		label="event_type",
		pattern=EVENT_TYPE_PATTERN,
		max_length=MAX_EVENT_TYPE_LENGTH,
	)


def normalize_endpoint(value: object, *, label: str) -> str:
	return _normalize_pattern(
		value,
		label=label,
		pattern=ENDPOINT_PATTERN,
		max_length=MAX_ENDPOINT_LENGTH,
	)


def normalize_payload_schema_version(value: object) -> int:
	if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 999:
		raise TransactionalMessageContractError("payload_schema_version is invalid")
	return value


def normalize_payload(value: object) -> tuple[dict[str, object], str, str]:
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except json.JSONDecodeError as exc:
			raise TransactionalMessageContractError("payload_json is invalid") from exc
	try:
		normalized, serialized = canonical_json_object(value)
	except DomainServiceContractError as exc:
		raise TransactionalMessageContractError(str(exc)) from exc
	return normalized, serialized, sha256(serialized.encode("utf-8")).hexdigest()


def normalize_optional_result(value: object | None) -> tuple[dict[str, object], str, str] | None:
	if value in (None, ""):
		return None
	return normalize_payload(value)


def normalize_optional_event_id(value: object | None, *, label: str) -> str | None:
	if value in (None, ""):
		return None
	try:
		return normalize_event_id(value)
	except TransactionalMessageContractError as exc:
		raise TransactionalMessageContractError(f"{label} is invalid") from exc


def normalize_reference(
	reference_doctype: object | None,
	reference_name: object | None,
) -> tuple[str | None, str | None]:
	if reference_doctype in (None, "") and reference_name in (None, ""):
		return None, None
	if (
		not isinstance(reference_doctype, str)
		or not reference_doctype
		or reference_doctype != reference_doctype.strip()
		or len(reference_doctype) > 140
		or any(ord(character) < 32 for character in reference_doctype)
		or not isinstance(reference_name, str)
		or not reference_name
		or reference_name != reference_name.strip()
		or len(reference_name) > 140
		or any(ord(character) < 32 for character in reference_name)
	):
		raise TransactionalMessageContractError("message reference is invalid")
	return reference_doctype, reference_name


def normalize_message_snapshot(
	*,
	event_id: object,
	event_type: object,
	source: object,
	destination: object,
	occurred_at: object,
	payload_schema_version: object,
	payload: object,
	correlation_id: object,
	causation_id: object | None = None,
	reference_doctype: object | None = None,
	reference_name: object | None = None,
) -> MessageSnapshot:
	if (
		not isinstance(occurred_at, str)
		or not occurred_at.strip()
		or occurred_at != occurred_at.strip()
		or len(occurred_at) > 40
	):
		raise TransactionalMessageContractError("occurred_at is invalid")
	try:
		datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
	except ValueError as exc:
		raise TransactionalMessageContractError("occurred_at is invalid") from exc
	if (
		not isinstance(correlation_id, str)
		or not correlation_id
		or correlation_id != correlation_id.strip()
		or len(correlation_id) > 140
		or any(ord(character) < 32 for character in correlation_id)
	):
		raise TransactionalMessageContractError("correlation_id is invalid")
	_, payload_json, payload_hash = normalize_payload(payload)
	normalized_reference = normalize_reference(reference_doctype, reference_name)
	return {
		"event_id": normalize_event_id(event_id),
		"event_type": normalize_event_type(event_type),
		"source": normalize_endpoint(source, label="source"),
		"destination": normalize_endpoint(destination, label="destination"),
		"occurred_at": occurred_at,
		"payload_schema_version": normalize_payload_schema_version(payload_schema_version),
		"payload_json": payload_json,
		"payload_hash": payload_hash,
		"correlation_id": correlation_id,
		"causation_id": normalize_optional_event_id(causation_id, label="causation_id"),
		"reference_doctype": normalized_reference[0],
		"reference_name": normalized_reference[1],
	}


def message_record_name(kind: MessageBoxKind, event_id: object, consumer: object | None = None) -> str:
	normalized_event_id = normalize_event_id(event_id)
	if kind == "outbox":
		if consumer not in (None, ""):
			raise TransactionalMessageContractError("outbox record cannot declare a consumer")
		identity = normalized_event_id
		prefix = "out"
	elif kind == "inbox":
		normalized_consumer = normalize_endpoint(consumer, label="consumer")
		identity = f"{normalized_consumer}\0{normalized_event_id}"
		prefix = "in"
	else:
		raise TransactionalMessageContractError("message box kind is invalid")
	return f"{prefix}-{sha256(identity.encode('utf-8')).hexdigest()}"


def normalize_processing_token(value: object) -> str:
	return PrivateText(
		_normalize_pattern(
			value,
			label="processing_token",
			pattern=PROCESSING_TOKEN_PATTERN,
			max_length=MAX_PROCESSING_TOKEN_LENGTH,
		)
	)


def processing_token_hash(value: object) -> str:
	return sha256(normalize_processing_token(value).encode("utf-8")).hexdigest()


def processing_token_matches(value: object, expected_hash: object) -> bool:
	try:
		normalized_hash = normalize_sha256(expected_hash, label="processing_token_hash")
	except DomainServiceContractError as exc:
		raise TransactionalMessageContractError("processing_token_hash is invalid") from exc
	return hmac.compare_digest(processing_token_hash(value), normalized_hash)


def normalize_error_details(
	error_code: object,
	error_message: object,
) -> tuple[str, str]:
	code = _normalize_pattern(
		error_code,
		label="error_code",
		pattern=ERROR_CODE_PATTERN,
		max_length=MAX_ERROR_CODE_LENGTH,
	)
	if (
		not isinstance(error_message, str)
		or not error_message
		or error_message != error_message.strip()
		or len(error_message) > MAX_ERROR_MESSAGE_LENGTH
		or any(ord(character) < 32 for character in error_message)
	):
		raise TransactionalMessageContractError("error_message is invalid")
	return code, error_message


def get_transactional_message_public_contract() -> TransactionalMessagePublicContract:
	return {
		"schema_version": MESSAGE_BOX_SCHEMA_VERSION,
		"base_fields": [field.as_public_dict() for field in BASE_MESSAGE_FIELDS],
		"statuses": {
			"outbox": list(OUTBOX_STATUSES),
			"inbox": list(INBOX_STATUSES),
		},
		"delivery_semantics": {
			"outbox_delivery": "at_least_once",
			"inbox_deduplication": "consumer_and_event_id",
			"payload_integrity": "canonical_json_sha256",
			"claiming": "row_lock_nowait_and_lease",
			"external_calls_inside_transaction": False,
		},
		"mutation_policy": {
			"service_only_insert": True,
			"service_only_transition": True,
			"direct_update": False,
			"delete": False,
			"rename": False,
		},
		"http_write_enabled": False,
	}


__all__ = [
	"BASE_MESSAGE_FIELDNAMES",
	"BASE_MESSAGE_FIELDS",
	"IMMUTABLE_MESSAGE_FIELDS",
	"INBOX_STATUSES",
	"MESSAGE_BOX_SCHEMA_VERSION",
	"MESSAGE_STATUSES",
	"OUTBOX_STATUSES",
	"MessageBoxDefinition",
	"MessageField",
	"MessageSnapshot",
	"TransactionalMessageContractError",
	"TransactionalMessagePublicContract",
	"get_transactional_message_public_contract",
	"message_record_name",
	"normalize_endpoint",
	"normalize_error_details",
	"normalize_event_id",
	"normalize_event_type",
	"normalize_message_snapshot",
	"normalize_optional_result",
	"normalize_payload",
	"normalize_payload_schema_version",
	"normalize_processing_token",
	"processing_token_hash",
	"processing_token_matches",
]
