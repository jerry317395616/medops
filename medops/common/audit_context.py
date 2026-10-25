from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

AUDIT_CONTEXT_SCHEMA_VERSION = 1
AUDIT_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,139}$")
AUDIT_EVENT_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
AUDIT_FIELD_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
AUDIT_LOGGER_PATTERN = re.compile(r"^medops(?:\.[a-z][a-z0-9_]*)*$")
AUDIT_CHANNELS = frozenset({"http", "job", "service"})
AUDIT_ORIGINS = frozenset({"generated", "header", "parameter", "propagated"})
PROPAGATION_KEYS = frozenset(
	{
		"schema_version",
		"correlation_id",
		"parent_request_id",
	}
)
RESERVED_AUDIT_FIELDS = frozenset(
	{
		"schema_version",
		"event",
		"correlation_id",
		"request_id",
		"parent_request_id",
		"channel",
	}
)
FORBIDDEN_AUDIT_FIELD_MARKERS = frozenset(
	{
		"bench",
		"email",
		"message",
		"password",
		"path",
		"patient",
		"payload",
		"secret",
		"site",
		"sql",
		"token",
		"user",
	}
)


class AuditContextError(ValueError):
	"""Raised when an audit identifier, carrier or event is unsafe."""


def normalize_audit_identifier(value: object, *, label: str) -> str:
	if not isinstance(value, str) or not value:
		raise AuditContextError(f"{label} must be a non-empty string")
	if value != value.strip() or AUDIT_IDENTIFIER_PATTERN.fullmatch(value) is None:
		raise AuditContextError(f"{label} is invalid")
	return value


def normalize_correlation_id(value: object) -> str:
	return normalize_audit_identifier(value, label="correlation_id")


def normalize_request_id(value: object) -> str:
	return normalize_audit_identifier(value, label="request_id")


def normalize_logger_name(value: object) -> str:
	if not isinstance(value, str) or AUDIT_LOGGER_PATTERN.fullmatch(value) is None:
		raise AuditContextError("logger_name is invalid")
	return value


@dataclass(frozen=True, slots=True)
class AuditContext:
	correlation_id: str
	request_id: str
	channel: str
	parent_request_id: str | None = None
	origin: str = "generated"

	def __post_init__(self) -> None:
		normalize_correlation_id(self.correlation_id)
		normalize_request_id(self.request_id)
		if self.parent_request_id is not None:
			normalize_request_id(self.parent_request_id)
		if self.channel not in AUDIT_CHANNELS:
			raise AuditContextError("channel is invalid")
		if self.origin not in AUDIT_ORIGINS:
			raise AuditContextError("origin is invalid")
		if self.parent_request_id == self.request_id:
			raise AuditContextError("parent_request_id must differ from request_id")

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": AUDIT_CONTEXT_SCHEMA_VERSION,
			"correlation_id": self.correlation_id,
			"request_id": self.request_id,
			"parent_request_id": self.parent_request_id,
			"channel": self.channel,
		}

	def as_audit_dict(self) -> dict[str, object]:
		return self.as_public_dict()

	def as_propagation_dict(self) -> dict[str, object]:
		return {
			"schema_version": AUDIT_CONTEXT_SCHEMA_VERSION,
			"correlation_id": self.correlation_id,
			"parent_request_id": self.request_id,
		}


def parse_propagation_payload(payload: object, *, request_id: str) -> AuditContext:
	if not isinstance(payload, dict) or frozenset(payload) != PROPAGATION_KEYS:
		raise AuditContextError("audit context carrier has invalid keys")
	if payload["schema_version"] != AUDIT_CONTEXT_SCHEMA_VERSION:
		raise AuditContextError("audit context carrier has unsupported schema_version")
	return AuditContext(
		correlation_id=normalize_correlation_id(payload["correlation_id"]),
		request_id=normalize_request_id(request_id),
		parent_request_id=normalize_request_id(payload["parent_request_id"]),
		channel="job",
		origin="propagated",
	)


def normalize_audit_event(event: object) -> str:
	if not isinstance(event, str) or AUDIT_EVENT_PATTERN.fullmatch(event) is None:
		raise AuditContextError("audit event is invalid")
	return event


def _contains_forbidden_marker(fieldname: str) -> bool:
	parts = frozenset(fieldname.split("_"))
	return bool(parts.intersection(FORBIDDEN_AUDIT_FIELD_MARKERS))


def _normalize_audit_value(value: object, *, label: str) -> str | int | float | bool | None:
	if value is None or isinstance(value, (str, int, float, bool)):
		if isinstance(value, str):
			if len(value) > 160 or any(ord(character) < 32 for character in value):
				raise AuditContextError(f"{label} contains an unsafe string")
		elif isinstance(value, float) and not math.isfinite(value):
			raise AuditContextError(f"{label} contains a non-finite number")
		return value
	raise AuditContextError(f"{label} must be a scalar")


def normalize_audit_fields(fields: dict[str, Any]) -> dict[str, object]:
	normalized: dict[str, object] = {}
	for fieldname in sorted(fields):
		if (
			AUDIT_FIELD_PATTERN.fullmatch(fieldname) is None
			or fieldname in RESERVED_AUDIT_FIELDS
			or _contains_forbidden_marker(fieldname)
		):
			raise AuditContextError(f"audit field is forbidden: {fieldname}")
		normalized[fieldname] = _normalize_audit_value(
			fields[fieldname],
			label=f"audit field {fieldname}",
		)
	return normalized
