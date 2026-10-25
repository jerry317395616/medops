from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENVIRONMENT_PROFILE_NAMES = ("development", "test", "demo")
REQUIRED_APPS = ("frappe", "erpnext", "hrms", "medops")
PROFILE_KEYS = frozenset(
	{
		"label",
		"bench_dir",
		"site_name",
		"developer_mode",
		"allow_tests",
		"scheduler_enabled",
		"external_integrations_enabled",
		"email_queue_enabled",
		"synthetic_data_only",
		"public_access",
		"reset_policy",
		"ports",
	}
)
PORT_KEYS = ("redis_cache", "redis_queue", "socketio", "webserver", "file_watcher")
SITE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$")
PROFILE_RUNTIME_POLICY = {
	"development": (True, True, True),
	"test": (False, True, False),
	"demo": (False, False, False),
}


class EnvironmentProfileError(ValueError):
	pass


@dataclass(frozen=True)
class EnvironmentPorts:
	redis_cache: int
	redis_queue: int
	socketio: int
	webserver: int
	file_watcher: int

	def values(self) -> tuple[int, ...]:
		return (
			self.redis_cache,
			self.redis_queue,
			self.socketio,
			self.webserver,
			self.file_watcher,
		)

	def as_dict(self) -> dict[str, int]:
		return {key: getattr(self, key) for key in PORT_KEYS}


@dataclass(frozen=True)
class EnvironmentProfile:
	name: str
	label: str
	bench_dir: str
	site_name: str
	developer_mode: bool
	allow_tests: bool
	scheduler_enabled: bool
	external_integrations_enabled: bool
	email_queue_enabled: bool
	synthetic_data_only: bool
	public_access: bool
	reset_policy: str
	ports: EnvironmentPorts

	def expanded_bench_dir(self) -> Path:
		return Path(self.bench_dir).expanduser().resolve()

	def expected_site_config(self, schema_version: int) -> dict[str, object]:
		return {
			"medops_environment": self.name,
			"medops_environment_schema_version": schema_version,
			"medops_synthetic_data_only": self.synthetic_data_only,
			"medops_external_integrations_enabled": self.external_integrations_enabled,
			"medops_public_access": self.public_access,
			"disable_email_queue": not self.email_queue_enabled,
			"developer_mode": self.developer_mode,
			"allow_tests": self.allow_tests,
		}

	def as_public_dict(self, schema_version: int) -> dict[str, object]:
		return {
			"managed": True,
			"name": self.name,
			"label": self.label,
			"schema_version": schema_version,
			"developer_mode": self.developer_mode,
			"allow_tests": self.allow_tests,
			"scheduler_enabled": self.scheduler_enabled,
			"external_integrations_enabled": self.external_integrations_enabled,
			"email_queue_enabled": self.email_queue_enabled,
			"synthetic_data_only": self.synthetic_data_only,
			"public_access": self.public_access,
			"reset_policy": self.reset_policy,
		}


@dataclass(frozen=True)
class EnvironmentRegistry:
	schema_version: int
	required_apps: tuple[str, ...]
	profiles: tuple[EnvironmentProfile, ...]

	def get(self, name: str) -> EnvironmentProfile:
		for profile in self.profiles:
			if profile.name == name:
				return profile
		raise EnvironmentProfileError(f"Unknown environment profile: {name}")


def _require_bool(payload: dict[str, Any], key: str, profile_name: str) -> bool:
	value = payload.get(key)
	if type(value) is not bool:
		raise EnvironmentProfileError(f"{profile_name}.{key} must be a boolean")
	return value


def _require_string(payload: dict[str, Any], key: str, profile_name: str) -> str:
	value = payload.get(key)
	if not isinstance(value, str) or not value.strip():
		raise EnvironmentProfileError(f"{profile_name}.{key} must be a non-empty string")
	return value.strip()


def _parse_ports(payload: object, profile_name: str) -> EnvironmentPorts:
	if not isinstance(payload, dict) or set(payload) != set(PORT_KEYS):
		raise EnvironmentProfileError(f"{profile_name}.ports must contain exactly {PORT_KEYS}")
	values: dict[str, int] = {}
	for key in PORT_KEYS:
		value = payload[key]
		if type(value) is not int or not 1024 <= value <= 65535:
			raise EnvironmentProfileError(f"{profile_name}.ports.{key} must be an unprivileged port")
		values[key] = value
	return EnvironmentPorts(**values)


def _parse_profile(name: str, payload: object) -> EnvironmentProfile:
	if not isinstance(payload, dict) or set(payload) != PROFILE_KEYS:
		raise EnvironmentProfileError(f"{name} must contain exactly {sorted(PROFILE_KEYS)}")
	return EnvironmentProfile(
		name=name,
		label=_require_string(payload, "label", name),
		bench_dir=_require_string(payload, "bench_dir", name),
		site_name=_require_string(payload, "site_name", name),
		developer_mode=_require_bool(payload, "developer_mode", name),
		allow_tests=_require_bool(payload, "allow_tests", name),
		scheduler_enabled=_require_bool(payload, "scheduler_enabled", name),
		external_integrations_enabled=_require_bool(
			payload,
			"external_integrations_enabled",
			name,
		),
		email_queue_enabled=_require_bool(payload, "email_queue_enabled", name),
		synthetic_data_only=_require_bool(payload, "synthetic_data_only", name),
		public_access=_require_bool(payload, "public_access", name),
		reset_policy=_require_string(payload, "reset_policy", name),
		ports=_parse_ports(payload.get("ports"), name),
	)


def _validate_profile(profile: EnvironmentProfile) -> list[str]:
	violations: list[str] = []
	if not SITE_NAME_PATTERN.fullmatch(profile.site_name) or not profile.site_name.endswith(".localhost"):
		violations.append(f"{profile.name}.site_name must be a .localhost name")
	if profile.name not in profile.bench_dir:
		violations.append(f"{profile.name}.bench_dir must include the profile name")
	if profile.reset_policy != "replaceable":
		violations.append(f"{profile.name}.reset_policy must be replaceable")
	if profile.external_integrations_enabled:
		violations.append(f"{profile.name} must disable external integrations")
	if profile.email_queue_enabled:
		violations.append(f"{profile.name} must disable the email queue")
	if not profile.synthetic_data_only:
		violations.append(f"{profile.name} must require synthetic data")
	if profile.public_access:
		violations.append(f"{profile.name} must not enable public access")
	expected_runtime = PROFILE_RUNTIME_POLICY[profile.name]
	actual_runtime = (
		profile.developer_mode,
		profile.allow_tests,
		profile.scheduler_enabled,
	)
	if actual_runtime != expected_runtime:
		violations.append(
			f"{profile.name} runtime policy must be developer_mode={expected_runtime[0]}, "
			f"allow_tests={expected_runtime[1]}, scheduler_enabled={expected_runtime[2]}"
		)
	return violations


def parse_environment_registry(payload: object) -> EnvironmentRegistry:
	if not isinstance(payload, dict):
		raise EnvironmentProfileError("Environment profile registry must be an object")
	if set(payload) != {"schema_version", "required_apps", "profiles"}:
		raise EnvironmentProfileError("Environment profile registry has unexpected keys")
	if payload["schema_version"] != 1:
		raise EnvironmentProfileError("Environment profile schema_version must be 1")
	required_apps = payload["required_apps"]
	if not isinstance(required_apps, list) or tuple(required_apps) != REQUIRED_APPS:
		raise EnvironmentProfileError(f"required_apps must be {REQUIRED_APPS}")
	profile_payloads = payload["profiles"]
	if not isinstance(profile_payloads, dict) or tuple(profile_payloads) != ENVIRONMENT_PROFILE_NAMES:
		raise EnvironmentProfileError(f"profiles must be ordered as {ENVIRONMENT_PROFILE_NAMES}")

	profiles = tuple(_parse_profile(name, profile_payloads[name]) for name in ENVIRONMENT_PROFILE_NAMES)
	violations = [violation for profile in profiles for violation in _validate_profile(profile)]
	bench_dirs = [profile.bench_dir for profile in profiles]
	site_names = [profile.site_name for profile in profiles]
	ports = [port for profile in profiles for port in profile.ports.values()]
	if len(set(bench_dirs)) != len(bench_dirs):
		violations.append("Environment bench directories must be unique")
	if len(set(site_names)) != len(site_names):
		violations.append("Environment site names must be unique")
	if len(set(ports)) != len(ports):
		violations.append("Environment service ports must be unique")
	if violations:
		raise EnvironmentProfileError("; ".join(violations))
	return EnvironmentRegistry(
		schema_version=payload["schema_version"],
		required_apps=tuple(required_apps),
		profiles=profiles,
	)


def load_environment_registry(path: Path | None = None) -> EnvironmentRegistry:
	profile_path = path or Path(__file__).resolve().parents[1] / "config" / "environment_profiles.json"
	try:
		payload = json.loads(profile_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise EnvironmentProfileError(f"Cannot load environment profiles: {exc}") from exc
	return parse_environment_registry(payload)
