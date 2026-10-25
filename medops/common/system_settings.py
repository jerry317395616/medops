from __future__ import annotations

from dataclasses import dataclass

SYSTEM_SETTINGS_SCHEMA_VERSION = 1
SYSTEM_SETTINGS_DOCTYPE = "HRP System Settings"
LOCKED_RELEASE_CHANNEL = "locked-develop"
MIN_INTEGRATION_TIMEOUT_SECONDS = 5
MAX_INTEGRATION_TIMEOUT_SECONDS = 300
MAX_REFERENCE_LENGTH = 140
MAX_REMARKS_LENGTH = 500


class SystemSettingsContractError(ValueError):
	"""Raised when system settings do not satisfy the public contract."""


def normalize_boolean(value: object, *, label: str) -> bool:
	if isinstance(value, bool):
		return value
	if isinstance(value, int) and value in (0, 1):
		return bool(value)
	if isinstance(value, str) and value in {"0", "1"}:
		return value == "1"
	raise SystemSettingsContractError(f"{label} must be boolean")


def normalize_positive_integer(value: object, *, label: str) -> int:
	if isinstance(value, bool):
		raise SystemSettingsContractError(f"{label} must be an integer")
	if isinstance(value, int):
		normalized = value
	elif isinstance(value, str) and value and value.isascii() and value.isdigit():
		normalized = int(value)
	else:
		raise SystemSettingsContractError(f"{label} must be an integer")
	if normalized < 1:
		raise SystemSettingsContractError(f"{label} must be positive")
	return normalized


def normalize_optional_reference(value: object, *, label: str) -> str | None:
	if value is None or value == "":
		return None
	if not isinstance(value, str) or value != value.strip():
		raise SystemSettingsContractError(f"{label} is invalid")
	if len(value) > MAX_REFERENCE_LENGTH or any(ord(character) < 32 for character in value):
		raise SystemSettingsContractError(f"{label} is invalid")
	return value


def normalize_optional_remarks(value: object) -> str | None:
	if value is None or value == "":
		return None
	if not isinstance(value, str):
		raise SystemSettingsContractError("remarks must be text")
	normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
	if not normalized:
		return None
	if len(normalized) > MAX_REMARKS_LENGTH:
		raise SystemSettingsContractError("remarks is too long")
	if any(ord(character) < 32 and character not in {"\n", "\t"} for character in normalized):
		raise SystemSettingsContractError("remarks contains control characters")
	return normalized


def normalize_timeout(value: object) -> int:
	normalized = normalize_positive_integer(value, label="integration_timeout_seconds")
	if not MIN_INTEGRATION_TIMEOUT_SECONDS <= normalized <= MAX_INTEGRATION_TIMEOUT_SECONDS:
		raise SystemSettingsContractError("integration_timeout_seconds is outside the allowed range")
	return normalized


@dataclass(frozen=True, slots=True)
class SystemSettingsUpdate:
	enabled: bool
	default_company: str | None
	default_hospital: str | None
	integration_timeout_seconds: int
	remarks: str | None
	expected_version: int

	def as_request_payload(self) -> dict[str, object]:
		return {
			"enabled": self.enabled,
			"default_company": self.default_company,
			"default_hospital": self.default_hospital,
			"integration_timeout_seconds": self.integration_timeout_seconds,
			"remarks": self.remarks,
			"expected_version": self.expected_version,
		}


def build_system_settings_update(
	*,
	enabled: object,
	default_company: object,
	default_hospital: object,
	integration_timeout_seconds: object,
	remarks: object,
	expected_version: object,
) -> SystemSettingsUpdate:
	company = normalize_optional_reference(default_company, label="default_company")
	hospital = normalize_optional_reference(default_hospital, label="default_hospital")
	if hospital is not None and company is None:
		raise SystemSettingsContractError("default_hospital requires default_company")
	return SystemSettingsUpdate(
		enabled=normalize_boolean(enabled, label="enabled"),
		default_company=company,
		default_hospital=hospital,
		integration_timeout_seconds=normalize_timeout(integration_timeout_seconds),
		remarks=normalize_optional_remarks(remarks),
		expected_version=normalize_positive_integer(
			expected_version,
			label="expected_version",
		),
	)


@dataclass(frozen=True, slots=True)
class SystemSettingsState:
	enabled: bool
	default_company: str | None
	default_hospital: str | None
	integration_timeout_seconds: int
	remarks: str | None
	configuration_version: int
	release_channel: str = LOCKED_RELEASE_CHANNEL
	strict_data_scope: bool = True
	require_human_confirmation_for_ai: bool = True

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": SYSTEM_SETTINGS_SCHEMA_VERSION,
			"doctype": SYSTEM_SETTINGS_DOCTYPE,
			"enabled": self.enabled,
			"release_channel": self.release_channel,
			"default_company": self.default_company,
			"default_hospital": self.default_hospital,
			"strict_data_scope": self.strict_data_scope,
			"require_human_confirmation_for_ai": self.require_human_confirmation_for_ai,
			"integration_timeout_seconds": self.integration_timeout_seconds,
			"remarks": self.remarks,
			"configuration_version": self.configuration_version,
		}


def build_system_settings_state(
	*,
	enabled: object,
	default_company: object,
	default_hospital: object,
	integration_timeout_seconds: object,
	remarks: object,
	configuration_version: object,
	release_channel: object,
	strict_data_scope: object,
	require_human_confirmation_for_ai: object,
) -> SystemSettingsState:
	update = build_system_settings_update(
		enabled=enabled,
		default_company=default_company,
		default_hospital=default_hospital,
		integration_timeout_seconds=integration_timeout_seconds,
		remarks=remarks,
		expected_version=configuration_version,
	)
	if release_channel != LOCKED_RELEASE_CHANNEL:
		raise SystemSettingsContractError("release_channel must use the locked baseline")
	if not normalize_boolean(strict_data_scope, label="strict_data_scope"):
		raise SystemSettingsContractError("strict_data_scope cannot be disabled")
	if not normalize_boolean(
		require_human_confirmation_for_ai,
		label="require_human_confirmation_for_ai",
	):
		raise SystemSettingsContractError("AI human confirmation cannot be disabled")
	return SystemSettingsState(
		enabled=update.enabled,
		default_company=update.default_company,
		default_hospital=update.default_hospital,
		integration_timeout_seconds=update.integration_timeout_seconds,
		remarks=update.remarks,
		configuration_version=update.expected_version,
	)


def changed_mutable_fields(
	current: SystemSettingsState,
	update: SystemSettingsUpdate,
) -> tuple[str, ...]:
	return tuple(
		fieldname
		for fieldname in (
			"enabled",
			"default_company",
			"default_hospital",
			"integration_timeout_seconds",
			"remarks",
		)
		if getattr(current, fieldname) != getattr(update, fieldname)
	)


__all__ = [
	"LOCKED_RELEASE_CHANNEL",
	"MAX_INTEGRATION_TIMEOUT_SECONDS",
	"MAX_REFERENCE_LENGTH",
	"MAX_REMARKS_LENGTH",
	"MIN_INTEGRATION_TIMEOUT_SECONDS",
	"SYSTEM_SETTINGS_DOCTYPE",
	"SYSTEM_SETTINGS_SCHEMA_VERSION",
	"SystemSettingsContractError",
	"SystemSettingsState",
	"SystemSettingsUpdate",
	"build_system_settings_state",
	"build_system_settings_update",
	"changed_mutable_fields",
	"normalize_boolean",
	"normalize_optional_reference",
	"normalize_optional_remarks",
	"normalize_positive_integer",
	"normalize_timeout",
]
