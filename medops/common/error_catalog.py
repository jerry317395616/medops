from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from medops.common.constants import APP_NAME

APP_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ERROR_CATALOG_PATH = APP_PACKAGE_ROOT / "config" / "error_catalog.json"
ZH_TRANSLATION_PATH = APP_PACKAGE_ROOT / "translations" / "zh.csv"
NAMESPACE = "IONE-CORE"
ROOT_KEYS = frozenset({"schema_version", "app", "namespace", "errors"})
ERROR_KEYS = frozenset(
	{
		"key",
		"code",
		"category",
		"http_status",
		"message",
		"retryable",
		"log_level",
	}
)
ERROR_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
ERROR_CODE_PATTERN = re.compile(r"^IONE-CORE-([0-9]{4})$")
ERROR_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,63}$")
ALLOWED_CATEGORIES = frozenset(
	{
		"authentication",
		"authorization",
		"validation",
		"not_found",
		"conflict",
		"policy",
		"dependency",
		"throttling",
		"internal",
	}
)
ALLOWED_STATUS_BY_CATEGORY = {
	"authentication": frozenset({401}),
	"authorization": frozenset({403}),
	"validation": frozenset({400, 422}),
	"not_found": frozenset({404}),
	"conflict": frozenset({409}),
	"policy": frozenset({403, 409}),
	"dependency": frozenset({502, 503, 504}),
	"throttling": frozenset({429}),
	"internal": frozenset({500}),
}
RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})
ALLOWED_LOG_LEVELS = frozenset({"warning", "error"})
TRANSLATABLE_SUPPORT_MESSAGES = ("Request failed",)


class ErrorCatalogError(ValueError):
	"""Raised when the source-controlled error contract is invalid."""


@dataclass(frozen=True)
class ErrorDefinition:
	key: str
	code: str
	category: str
	http_status: int
	message: str
	retryable: bool
	log_level: str

	def as_public_dict(self, translate: Callable[[str], str] | None = None) -> dict[str, object]:
		message = translate(self.message) if translate is not None else self.message
		return {
			"code": self.code,
			"category": self.category,
			"http_status": self.http_status,
			"message": message,
			"retryable": self.retryable,
		}


@dataclass(frozen=True)
class ErrorCatalog:
	schema_version: int
	app: str
	namespace: str
	errors: tuple[ErrorDefinition, ...]
	sha256: str

	@property
	def by_key(self) -> dict[str, ErrorDefinition]:
		return {definition.key: definition for definition in self.errors}

	@property
	def by_code(self) -> dict[str, ErrorDefinition]:
		return {definition.code: definition for definition in self.errors}

	def get(self, key_or_code: str) -> ErrorDefinition:
		definition = self.by_key.get(key_or_code) or self.by_code.get(key_or_code)
		if definition is None:
			raise ErrorCatalogError(f"Unknown I-ONE error: {key_or_code}")
		return definition

	def as_public_dict(self, translate: Callable[[str], str] | None = None) -> dict[str, object]:
		return {
			"status": "ok",
			"schema_version": self.schema_version,
			"namespace": self.namespace,
			"error_count": len(self.errors),
			"sha256": self.sha256,
			"errors": [definition.as_public_dict(translate) for definition in self.errors],
		}


class IoneApplicationError(Exception):
	"""Stable application error that Frappe can convert to an HTTP response."""

	def __init__(
		self,
		definition: ErrorDefinition,
		error_id: str,
		*,
		public_message: str | None = None,
	) -> None:
		if ERROR_ID_PATTERN.fullmatch(error_id) is None:
			raise ErrorCatalogError("error_id is invalid")
		self.definition = definition
		self.error_id = error_id
		self.public_message = public_message or definition.message
		self.http_status_code = definition.http_status
		self.skip_error_log = definition.http_status < 500
		super().__init__(self.public_message)

	@property
	def code(self) -> str:
		return self.definition.code

	@property
	def category(self) -> str:
		return self.definition.category

	@property
	def retryable(self) -> bool:
		return self.definition.retryable

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": 1,
			"code": self.code,
			"category": self.category,
			"message": self.public_message,
			"error_id": self.error_id,
			"retryable": self.retryable,
		}


def _require_exact_keys(payload: dict[str, Any], expected: frozenset[str], label: str) -> None:
	actual = frozenset(payload)
	if actual != expected:
		missing = sorted(expected - actual)
		extra = sorted(actual - expected)
		raise ErrorCatalogError(f"{label} keys mismatch; missing={missing}, extra={extra}")


def _require_string(value: object, label: str, *, max_length: int) -> str:
	if not isinstance(value, str) or not value.strip():
		raise ErrorCatalogError(f"{label} must be a non-empty string")
	normalized = value.strip()
	if normalized != value or len(normalized) > max_length:
		raise ErrorCatalogError(f"{label} must be trimmed and at most {max_length} characters")
	return normalized


def _parse_definition(payload: object, index: int) -> ErrorDefinition:
	label = f"errors[{index}]"
	if not isinstance(payload, dict):
		raise ErrorCatalogError(f"{label} must be an object")
	_require_exact_keys(payload, ERROR_KEYS, label)
	key = _require_string(payload["key"], f"{label}.key", max_length=64)
	if ERROR_KEY_PATTERN.fullmatch(key) is None:
		raise ErrorCatalogError(f"{label}.key is invalid")
	code = _require_string(payload["code"], f"{label}.code", max_length=32)
	match = ERROR_CODE_PATTERN.fullmatch(code)
	if match is None or int(match.group(1)) != index:
		raise ErrorCatalogError(f"{label}.code must be {NAMESPACE}-{index:04d}")
	category = _require_string(payload["category"], f"{label}.category", max_length=32)
	if category not in ALLOWED_CATEGORIES:
		raise ErrorCatalogError(f"{label}.category is not supported")
	http_status = payload["http_status"]
	if type(http_status) is not int or http_status not in ALLOWED_STATUS_BY_CATEGORY[category]:
		raise ErrorCatalogError(f"{label}.http_status is invalid for category {category}")
	message = _require_string(payload["message"], f"{label}.message", max_length=160)
	if (
		not message.isascii()
		or not message.endswith(".")
		or "\n" in message
		or "<" in message
		or ">" in message
		or "{" in message
		or "}" in message
	):
		raise ErrorCatalogError(f"{label}.message must be a static, single-line English sentence")
	retryable = payload["retryable"]
	if type(retryable) is not bool or retryable != (http_status in RETRYABLE_HTTP_STATUSES):
		raise ErrorCatalogError(f"{label}.retryable is inconsistent with http_status")
	log_level = _require_string(payload["log_level"], f"{label}.log_level", max_length=16)
	if log_level not in ALLOWED_LOG_LEVELS:
		raise ErrorCatalogError(f"{label}.log_level is not supported")
	expected_log_level = "error" if http_status >= 500 else "warning"
	if log_level != expected_log_level:
		raise ErrorCatalogError(f"{label}.log_level must be {expected_log_level}")
	return ErrorDefinition(
		key=key,
		code=code,
		category=category,
		http_status=http_status,
		message=message,
		retryable=retryable,
		log_level=log_level,
	)


def parse_error_catalog(payload: object) -> ErrorCatalog:
	if not isinstance(payload, dict):
		raise ErrorCatalogError("Error catalog root must be an object")
	_require_exact_keys(payload, ROOT_KEYS, "Error catalog")
	if payload["schema_version"] != 1:
		raise ErrorCatalogError("Error catalog schema_version must be 1")
	if payload["app"] != APP_NAME:
		raise ErrorCatalogError(f"Error catalog app must be {APP_NAME}")
	if payload["namespace"] != NAMESPACE:
		raise ErrorCatalogError(f"Error catalog namespace must be {NAMESPACE}")
	raw_errors = payload["errors"]
	if not isinstance(raw_errors, list) or not raw_errors:
		raise ErrorCatalogError("Error catalog errors must be a non-empty list")
	errors = tuple(
		_parse_definition(raw_definition, index) for index, raw_definition in enumerate(raw_errors, start=1)
	)
	keys = [definition.key for definition in errors]
	codes = [definition.code for definition in errors]
	messages = [definition.message for definition in errors]
	if len(set(keys)) != len(keys):
		raise ErrorCatalogError("Error catalog contains duplicate keys")
	if len(set(codes)) != len(codes):
		raise ErrorCatalogError("Error catalog contains duplicate codes")
	if len(set(messages)) != len(messages):
		raise ErrorCatalogError("Error catalog contains duplicate public messages")
	canonical = json.dumps(
		payload,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
	)
	return ErrorCatalog(
		schema_version=1,
		app=APP_NAME,
		namespace=NAMESPACE,
		errors=errors,
		sha256=hashlib.sha256(canonical.encode()).hexdigest(),
	)


def load_error_catalog(path: Path = ERROR_CATALOG_PATH) -> ErrorCatalog:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise ErrorCatalogError(f"Cannot load error catalog: {path.name}") from exc
	return parse_error_catalog(payload)


def load_translation_map(path: Path = ZH_TRANSLATION_PATH) -> dict[str, str]:
	try:
		with path.open(encoding="utf-8", newline="") as source:
			rows = list(csv.reader(source))
	except OSError as exc:
		raise ErrorCatalogError(f"Cannot load error translations: {path.name}") from exc
	translations: dict[str, str] = {}
	for index, row in enumerate(rows, start=1):
		if len(row) != 3:
			raise ErrorCatalogError(f"{path.name} row {index} must have exactly three columns")
		source, translation, context = row
		if not source or not translation or context:
			raise ErrorCatalogError(
				f"{path.name} row {index} requires source and translation with empty context"
			)
		if source in translations:
			raise ErrorCatalogError(f"{path.name} contains duplicate source {source}")
		translations[source] = translation
	return translations


def validate_error_translations(
	catalog: ErrorCatalog,
	path: Path = ZH_TRANSLATION_PATH,
) -> dict[str, str]:
	translations = load_translation_map(path)
	required = {definition.message for definition in catalog.errors}
	required.update(TRANSLATABLE_SUPPORT_MESSAGES)
	missing = sorted(required - set(translations))
	if missing:
		raise ErrorCatalogError(f"Chinese error translations are missing: {missing}")
	return translations
