from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

DOMAIN_SERVICE_SCHEMA_VERSION = 1
IDEMPOTENCY_RESPONSE_SCHEMA_VERSION = 1
MAX_IDEMPOTENCY_KEY_LENGTH = 140
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4096
MAX_JSON_STRING_LENGTH = 32768
MAX_JSON_SNAPSHOT_BYTES = 262144
SERVICE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.]{2,79}$")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,139}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ServiceKind = Literal["command", "query"]


class DomainServiceContractError(ValueError):
	"""Raised when a service definition or serialized contract is unsafe."""


def normalize_service_name(value: object) -> str:
	if not isinstance(value, str) or SERVICE_NAME_PATTERN.fullmatch(value) is None:
		raise DomainServiceContractError("service_name is invalid")
	return value


def normalize_idempotency_key(value: object) -> str:
	if (
		not isinstance(value, str)
		or value != value.strip()
		or len(value) > MAX_IDEMPOTENCY_KEY_LENGTH
		or IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None
	):
		raise DomainServiceContractError("idempotency_key is invalid")
	return value


def normalize_sha256(value: object, *, label: str) -> str:
	if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
		raise DomainServiceContractError(f"{label} is invalid")
	return value


def _normalize_json_value(value: object, *, depth: int, node_count: list[int]) -> object:
	if depth > MAX_JSON_DEPTH:
		raise DomainServiceContractError("JSON snapshot exceeds maximum depth")
	node_count[0] += 1
	if node_count[0] > MAX_JSON_NODES:
		raise DomainServiceContractError("JSON snapshot exceeds maximum nodes")
	if value is None or isinstance(value, (bool, int)):
		return value
	if isinstance(value, float):
		if not math.isfinite(value):
			raise DomainServiceContractError("JSON snapshot contains a non-finite number")
		return value
	if isinstance(value, str):
		if len(value) > MAX_JSON_STRING_LENGTH:
			raise DomainServiceContractError("JSON snapshot contains an oversized string")
		try:
			value.encode("utf-8")
		except UnicodeEncodeError as exc:
			raise DomainServiceContractError("JSON snapshot contains invalid Unicode") from exc
		return value
	if isinstance(value, list | tuple):
		return [_normalize_json_value(item, depth=depth + 1, node_count=node_count) for item in value]
	if isinstance(value, dict):
		normalized: dict[str, object] = {}
		keys = tuple(value)
		for key in keys:
			if (
				not isinstance(key, str)
				or not key
				or len(key) > 128
				or any(ord(character) < 32 for character in key)
			):
				raise DomainServiceContractError("JSON snapshot contains an invalid key")
			try:
				key.encode("utf-8")
			except UnicodeEncodeError as exc:
				raise DomainServiceContractError("JSON snapshot contains invalid Unicode") from exc
		for key in sorted(keys):
			normalized[key] = _normalize_json_value(
				value[key],
				depth=depth + 1,
				node_count=node_count,
			)
		return normalized
	raise DomainServiceContractError("JSON snapshot contains an unsupported value")


def canonical_json(value: object) -> str:
	normalized = _normalize_json_value(value, depth=0, node_count=[0])
	serialized = json.dumps(
		normalized,
		ensure_ascii=False,
		allow_nan=False,
		separators=(",", ":"),
		sort_keys=True,
	)
	if len(serialized.encode("utf-8")) > MAX_JSON_SNAPSHOT_BYTES:
		raise DomainServiceContractError("JSON snapshot exceeds maximum bytes")
	return serialized


def canonical_json_object(value: object) -> tuple[dict[str, object], str]:
	serialized = canonical_json(value)
	normalized = json.loads(serialized)
	if not isinstance(normalized, dict):
		raise DomainServiceContractError("service snapshot must be a JSON object")
	return normalized, serialized


def fingerprint_json(value: object) -> str:
	return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def idempotency_key_hash(idempotency_key: str) -> str:
	normalized = normalize_idempotency_key(idempotency_key)
	return sha256(normalized.encode("utf-8")).hexdigest()


def idempotency_record_name(service_name: str, idempotency_key: str) -> str:
	normalized_service = normalize_service_name(service_name)
	normalized_key = normalize_idempotency_key(idempotency_key)
	digest = sha256(f"{normalized_service}\0{normalized_key}".encode()).hexdigest()
	return f"idp-{digest}"


@dataclass(frozen=True, slots=True)
class DomainServiceDefinition:
	name: str
	version: int
	kind: ServiceKind
	required_roles: frozenset[str]
	idempotency_ttl_seconds: int = 86400

	def __post_init__(self) -> None:
		normalize_service_name(self.name)
		if (
			not isinstance(self.version, int)
			or isinstance(self.version, bool)
			or not 1 <= self.version <= 999
		):
			raise DomainServiceContractError("service version is invalid")
		if self.kind not in {"command", "query"}:
			raise DomainServiceContractError("service kind is invalid")
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
			raise DomainServiceContractError("required_roles is invalid")
		if not 60 <= self.idempotency_ttl_seconds <= 604800:
			raise DomainServiceContractError("idempotency_ttl_seconds is invalid")

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": DOMAIN_SERVICE_SCHEMA_VERSION,
			"name": self.name,
			"version": self.version,
			"kind": self.kind,
			"idempotency_required": self.kind == "command",
		}


@dataclass(frozen=True, slots=True)
class DomainServiceExecution:
	service: str
	service_version: int
	result: dict[str, object]
	correlation_id: str
	request_id: str
	idempotency_replayed: bool

	def as_public_dict(self) -> dict[str, object]:
		return {
			"service": self.service,
			"service_version": self.service_version,
			"result": dict(self.result),
			"correlation_id": self.correlation_id,
			"request_id": self.request_id,
			"idempotency_replayed": self.idempotency_replayed,
		}


__all__ = [
	"DOMAIN_SERVICE_SCHEMA_VERSION",
	"IDEMPOTENCY_RESPONSE_SCHEMA_VERSION",
	"DomainServiceContractError",
	"DomainServiceDefinition",
	"DomainServiceExecution",
	"canonical_json",
	"canonical_json_object",
	"fingerprint_json",
	"idempotency_key_hash",
	"idempotency_record_name",
	"normalize_idempotency_key",
	"normalize_service_name",
	"normalize_sha256",
]
