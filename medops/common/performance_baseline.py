from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from medops.common.constants import APP_NAME
from medops.common.domain_service import DomainServiceContractError

PERFORMANCE_BASELINE_SCHEMA_VERSION = 1
APP_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PERFORMANCE_BASELINES_PATH = APP_PACKAGE_ROOT / "config" / "performance_baselines.json"
PERFORMANCE_BASELINE_ROLES = ("System Manager", "HRP System Manager")
PERFORMANCE_ALLOWED_ENVIRONMENTS = ("development", "test")
SUPPORTED_RESPONSE_CONTRACTS = ("module_registry_v1",)
SUPPORTED_PROFILES = ("smoke", "baseline", "load")
SCENARIO_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SAFE_PATH_PATTERN = re.compile(r"^/api/method/medops\.[a-z0-9_.]+$")
ROOT_KEYS = frozenset({"schema_version", "app", "policy", "scenarios"})
POLICY_KEYS = frozenset(
	{
		"allowed_profiles",
		"managed_environment_required",
		"allow_tests_required",
		"synthetic_data_only_required",
		"public_access_forbidden",
		"external_integrations_forbidden",
		"http_write_enabled",
		"k6_version",
		"max_virtual_users",
		"max_iterations",
		"max_duration_seconds",
	}
)
SCENARIO_KEYS = frozenset(
	{
		"scenario_id",
		"version",
		"label",
		"description",
		"method",
		"path",
		"response_contract",
		"required_roles",
		"contains_personal_data",
		"read_only",
		"profiles",
	}
)
PROFILE_KEYS = frozenset(
	{
		"profile",
		"virtual_users",
		"iterations",
		"max_duration_seconds",
		"thresholds",
	}
)
THRESHOLD_KEYS = frozenset(
	{
		"max_error_rate",
		"min_check_rate",
		"p95_ms",
		"p99_ms",
		"min_requests_per_second",
	}
)
SUMMARY_KEYS = frozenset(
	{
		"schema_version",
		"scenario_id",
		"scenario_version",
		"profile",
		"registry_sha256",
		"tool_version",
		"run_id",
		"metrics",
	}
)
SUMMARY_METRIC_KEYS = frozenset(
	{
		"request_count",
		"error_rate",
		"check_rate",
		"requests_per_second",
		"p95_ms",
		"p99_ms",
		"duration_ms",
	}
)


class PerformanceBaselineContractError(DomainServiceContractError):
	"""Raised when a performance registry or result violates the source contract."""


def _require_exact_keys(payload: dict[str, Any], expected: frozenset[str], label: str) -> None:
	actual = frozenset(payload)
	if actual != expected:
		missing = sorted(expected - actual)
		extra = sorted(actual - expected)
		raise PerformanceBaselineContractError(f"{label} keys mismatch; missing={missing}, extra={extra}")


def _require_string(value: object, label: str, *, max_length: int) -> str:
	if (
		not isinstance(value, str)
		or not value
		or value != value.strip()
		or len(value) > max_length
		or any(ord(character) < 32 for character in value)
	):
		raise PerformanceBaselineContractError(f"{label} is invalid")
	return value


def _require_bool(value: object, label: str, *, expected: bool | None = None) -> bool:
	if type(value) is not bool:
		raise PerformanceBaselineContractError(f"{label} must be a boolean")
	if expected is not None and value is not expected:
		raise PerformanceBaselineContractError(f"{label} must be {str(expected).lower()}")
	return value


def _require_int(
	value: object,
	label: str,
	*,
	minimum: int,
	maximum: int,
) -> int:
	if type(value) is not int or not minimum <= value <= maximum:
		raise PerformanceBaselineContractError(f"{label} must be an integer from {minimum} to {maximum}")
	return value


def _require_number(
	value: object,
	label: str,
	*,
	minimum: float,
	maximum: float,
) -> float:
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		raise PerformanceBaselineContractError(f"{label} must be a finite number from {minimum} to {maximum}")
	number = float(value)
	if not math.isfinite(number) or not minimum <= number <= maximum:
		raise PerformanceBaselineContractError(f"{label} must be a finite number from {minimum} to {maximum}")
	return number


def _require_string_list(
	value: object,
	label: str,
	*,
	allowed: tuple[str, ...],
	max_items: int = 8,
) -> tuple[str, ...]:
	if not isinstance(value, list) or not 1 <= len(value) <= max_items:
		raise PerformanceBaselineContractError(f"{label} must be a bounded list")
	items = tuple(_require_string(item, label, max_length=80) for item in value)
	if len(set(items)) != len(items) or any(item not in allowed for item in items):
		raise PerformanceBaselineContractError(f"{label} contains unsupported or duplicate values")
	return items


def _require_semantic_version(value: object, label: str) -> str:
	version = _require_string(value, label, max_length=20)
	if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
		raise PerformanceBaselineContractError(f"{label} must be a semantic version")
	return version


@dataclass(frozen=True, slots=True)
class PerformanceThresholds:
	max_error_rate: float
	min_check_rate: float
	p95_ms: float
	p99_ms: float
	min_requests_per_second: float

	def as_public_dict(self) -> dict[str, float]:
		return {
			"max_error_rate": self.max_error_rate,
			"min_check_rate": self.min_check_rate,
			"p95_ms": self.p95_ms,
			"p99_ms": self.p99_ms,
			"min_requests_per_second": self.min_requests_per_second,
		}


@dataclass(frozen=True, slots=True)
class PerformanceLoadProfile:
	profile: str
	virtual_users: int
	iterations: int
	max_duration_seconds: int
	thresholds: PerformanceThresholds

	def as_public_dict(self) -> dict[str, object]:
		return {
			"profile": self.profile,
			"virtual_users": self.virtual_users,
			"iterations": self.iterations,
			"max_duration_seconds": self.max_duration_seconds,
			"thresholds": self.thresholds.as_public_dict(),
		}


@dataclass(frozen=True, slots=True)
class PerformanceScenarioDefinition:
	scenario_id: str
	version: int
	label: str
	description: str
	method: str
	path: str
	response_contract: str
	required_roles: tuple[str, ...]
	contains_personal_data: bool
	read_only: bool
	profiles: tuple[PerformanceLoadProfile, ...]

	def get_profile(self, profile_name: str) -> PerformanceLoadProfile:
		for profile in self.profiles:
			if profile.profile == profile_name:
				return profile
		raise PerformanceBaselineContractError(
			f"unknown performance profile for {self.scenario_id}: {profile_name}"
		)

	def as_public_dict(self) -> dict[str, object]:
		return {
			"scenario_id": self.scenario_id,
			"version": self.version,
			"label": self.label,
			"description": self.description,
			"method": self.method,
			"path": self.path,
			"response_contract": self.response_contract,
			"required_roles": list(self.required_roles),
			"contains_personal_data": self.contains_personal_data,
			"read_only": self.read_only,
			"profiles": [profile.as_public_dict() for profile in self.profiles],
		}


@dataclass(frozen=True, slots=True)
class PerformanceExecutionPolicy:
	allowed_profiles: tuple[str, ...]
	managed_environment_required: bool
	allow_tests_required: bool
	synthetic_data_only_required: bool
	public_access_forbidden: bool
	external_integrations_forbidden: bool
	http_write_enabled: bool
	k6_version: str
	max_virtual_users: int
	max_iterations: int
	max_duration_seconds: int

	def as_public_dict(self) -> dict[str, object]:
		return {
			"allowed_profiles": list(self.allowed_profiles),
			"managed_environment_required": self.managed_environment_required,
			"allow_tests_required": self.allow_tests_required,
			"synthetic_data_only_required": self.synthetic_data_only_required,
			"public_access_forbidden": self.public_access_forbidden,
			"external_integrations_forbidden": self.external_integrations_forbidden,
			"http_write_enabled": self.http_write_enabled,
			"k6_version": self.k6_version,
			"max_virtual_users": self.max_virtual_users,
			"max_iterations": self.max_iterations,
			"max_duration_seconds": self.max_duration_seconds,
			"idempotency": "read_only_replay_safe",
			"execution_location": "external_k6_process",
		}


@dataclass(frozen=True, slots=True)
class PerformanceBaselineRegistry:
	schema_version: int
	app: str
	policy: PerformanceExecutionPolicy
	scenarios: tuple[PerformanceScenarioDefinition, ...]
	sha256: str

	def get(self, scenario_id: str) -> PerformanceScenarioDefinition:
		for scenario in self.scenarios:
			if scenario.scenario_id == scenario_id:
				return scenario
		raise PerformanceBaselineContractError(f"unknown performance scenario: {scenario_id}")

	def as_public_dict(self) -> dict[str, object]:
		return {
			"status": "ok",
			"schema_version": self.schema_version,
			"app": self.app,
			"sha256": self.sha256,
			"scenario_count": len(self.scenarios),
			"scenarios": [scenario.as_public_dict() for scenario in self.scenarios],
			"execution_policy": self.policy.as_public_dict(),
			"result_policy": {
				"thresholds_enforced_by_k6": True,
				"independent_python_evaluation": True,
				"raw_credentials_persisted": False,
				"target_url_persisted": False,
			},
		}


@dataclass(frozen=True, slots=True)
class PerformanceRunMetrics:
	request_count: int
	error_rate: float
	check_rate: float
	requests_per_second: float
	p95_ms: float
	p99_ms: float
	duration_ms: float

	def as_dict(self) -> dict[str, int | float]:
		return {
			"request_count": self.request_count,
			"error_rate": self.error_rate,
			"check_rate": self.check_rate,
			"requests_per_second": self.requests_per_second,
			"p95_ms": self.p95_ms,
			"p99_ms": self.p99_ms,
			"duration_ms": self.duration_ms,
		}


@dataclass(frozen=True, slots=True)
class PerformanceRunSummary:
	schema_version: int
	scenario_id: str
	scenario_version: int
	profile: str
	registry_sha256: str
	tool_version: str
	run_id: str
	metrics: PerformanceRunMetrics

	def as_dict(self) -> dict[str, object]:
		return {
			"schema_version": self.schema_version,
			"scenario_id": self.scenario_id,
			"scenario_version": self.scenario_version,
			"profile": self.profile,
			"registry_sha256": self.registry_sha256,
			"tool_version": self.tool_version,
			"run_id": self.run_id,
			"metrics": self.metrics.as_dict(),
		}


def _parse_policy(payload: object) -> PerformanceExecutionPolicy:
	if not isinstance(payload, dict):
		raise PerformanceBaselineContractError("performance policy must be an object")
	_require_exact_keys(payload, POLICY_KEYS, "performance policy")
	return PerformanceExecutionPolicy(
		allowed_profiles=_require_string_list(
			payload["allowed_profiles"],
			"performance policy.allowed_profiles",
			allowed=PERFORMANCE_ALLOWED_ENVIRONMENTS,
		),
		managed_environment_required=_require_bool(
			payload["managed_environment_required"],
			"performance policy.managed_environment_required",
			expected=True,
		),
		allow_tests_required=_require_bool(
			payload["allow_tests_required"],
			"performance policy.allow_tests_required",
			expected=True,
		),
		synthetic_data_only_required=_require_bool(
			payload["synthetic_data_only_required"],
			"performance policy.synthetic_data_only_required",
			expected=True,
		),
		public_access_forbidden=_require_bool(
			payload["public_access_forbidden"],
			"performance policy.public_access_forbidden",
			expected=True,
		),
		external_integrations_forbidden=_require_bool(
			payload["external_integrations_forbidden"],
			"performance policy.external_integrations_forbidden",
			expected=True,
		),
		http_write_enabled=_require_bool(
			payload["http_write_enabled"],
			"performance policy.http_write_enabled",
			expected=False,
		),
		k6_version=_require_semantic_version(
			payload["k6_version"],
			"performance policy.k6_version",
		),
		max_virtual_users=_require_int(
			payload["max_virtual_users"],
			"performance policy.max_virtual_users",
			minimum=1,
			maximum=100,
		),
		max_iterations=_require_int(
			payload["max_iterations"],
			"performance policy.max_iterations",
			minimum=1,
			maximum=100_000,
		),
		max_duration_seconds=_require_int(
			payload["max_duration_seconds"],
			"performance policy.max_duration_seconds",
			minimum=1,
			maximum=1800,
		),
	)


def _parse_thresholds(payload: object, label: str) -> PerformanceThresholds:
	if not isinstance(payload, dict):
		raise PerformanceBaselineContractError(f"{label} must be an object")
	_require_exact_keys(payload, THRESHOLD_KEYS, label)
	thresholds = PerformanceThresholds(
		max_error_rate=_require_number(
			payload["max_error_rate"],
			f"{label}.max_error_rate",
			minimum=0,
			maximum=0.1,
		),
		min_check_rate=_require_number(
			payload["min_check_rate"],
			f"{label}.min_check_rate",
			minimum=0.9,
			maximum=1,
		),
		p95_ms=_require_number(payload["p95_ms"], f"{label}.p95_ms", minimum=1, maximum=30_000),
		p99_ms=_require_number(payload["p99_ms"], f"{label}.p99_ms", minimum=1, maximum=60_000),
		min_requests_per_second=_require_number(
			payload["min_requests_per_second"],
			f"{label}.min_requests_per_second",
			minimum=0.01,
			maximum=10_000,
		),
	)
	if thresholds.p95_ms > thresholds.p99_ms:
		raise PerformanceBaselineContractError(f"{label}.p95_ms must not exceed p99_ms")
	return thresholds


def _parse_profile(
	payload: object,
	label: str,
	policy: PerformanceExecutionPolicy,
) -> PerformanceLoadProfile:
	if not isinstance(payload, dict):
		raise PerformanceBaselineContractError(f"{label} must be an object")
	_require_exact_keys(payload, PROFILE_KEYS, label)
	profile_name = _require_string(payload["profile"], f"{label}.profile", max_length=32)
	if profile_name not in SUPPORTED_PROFILES:
		raise PerformanceBaselineContractError(f"{label}.profile is unsupported")
	virtual_users = _require_int(
		payload["virtual_users"],
		f"{label}.virtual_users",
		minimum=1,
		maximum=policy.max_virtual_users,
	)
	iterations = _require_int(
		payload["iterations"],
		f"{label}.iterations",
		minimum=virtual_users,
		maximum=policy.max_iterations,
	)
	max_duration_seconds = _require_int(
		payload["max_duration_seconds"],
		f"{label}.max_duration_seconds",
		minimum=1,
		maximum=policy.max_duration_seconds,
	)
	thresholds = _parse_thresholds(payload["thresholds"], f"{label}.thresholds")
	maximum_sustainable_rate = iterations / max_duration_seconds
	if thresholds.min_requests_per_second > maximum_sustainable_rate:
		raise PerformanceBaselineContractError(
			f"{label}.min_requests_per_second exceeds the bounded run rate"
		)
	return PerformanceLoadProfile(
		profile=profile_name,
		virtual_users=virtual_users,
		iterations=iterations,
		max_duration_seconds=max_duration_seconds,
		thresholds=thresholds,
	)


def _parse_scenario(
	payload: object,
	index: int,
	policy: PerformanceExecutionPolicy,
) -> PerformanceScenarioDefinition:
	label = f"scenarios[{index}]"
	if not isinstance(payload, dict):
		raise PerformanceBaselineContractError(f"{label} must be an object")
	_require_exact_keys(payload, SCENARIO_KEYS, label)
	scenario_id = _require_string(payload["scenario_id"], f"{label}.scenario_id", max_length=64)
	if SCENARIO_ID_PATTERN.fullmatch(scenario_id) is None:
		raise PerformanceBaselineContractError(f"{label}.scenario_id is invalid")
	version = _require_int(payload["version"], f"{label}.version", minimum=1, maximum=999)
	method = _require_string(payload["method"], f"{label}.method", max_length=8)
	if method != "GET":
		raise PerformanceBaselineContractError(f"{label}.method must be GET")
	path = _require_string(payload["path"], f"{label}.path", max_length=180)
	if SAFE_PATH_PATTERN.fullmatch(path) is None or ".." in path or "?" in path or "#" in path:
		raise PerformanceBaselineContractError(f"{label}.path is unsafe")
	response_contract = _require_string(
		payload["response_contract"],
		f"{label}.response_contract",
		max_length=64,
	)
	if response_contract not in SUPPORTED_RESPONSE_CONTRACTS:
		raise PerformanceBaselineContractError(f"{label}.response_contract is unsupported")
	required_roles = _require_string_list(
		payload["required_roles"],
		f"{label}.required_roles",
		allowed=PERFORMANCE_BASELINE_ROLES,
	)
	_require_bool(
		payload["contains_personal_data"],
		f"{label}.contains_personal_data",
		expected=False,
	)
	_require_bool(payload["read_only"], f"{label}.read_only", expected=True)
	raw_profiles = payload["profiles"]
	if not isinstance(raw_profiles, list) or not 1 <= len(raw_profiles) <= len(SUPPORTED_PROFILES):
		raise PerformanceBaselineContractError(f"{label}.profiles must be a bounded list")
	profiles = tuple(
		_parse_profile(profile, f"{label}.profiles[{profile_index}]", policy)
		for profile_index, profile in enumerate(raw_profiles)
	)
	profile_names = tuple(profile.profile for profile in profiles)
	if profile_names != SUPPORTED_PROFILES[: len(profile_names)]:
		raise PerformanceBaselineContractError(
			f"{label}.profiles must be ordered as {SUPPORTED_PROFILES[: len(profile_names)]}"
		)
	return PerformanceScenarioDefinition(
		scenario_id=scenario_id,
		version=version,
		label=_require_string(payload["label"], f"{label}.label", max_length=140),
		description=_require_string(payload["description"], f"{label}.description", max_length=500),
		method="GET",
		path=path,
		response_contract=response_contract,
		required_roles=required_roles,
		contains_personal_data=False,
		read_only=True,
		profiles=profiles,
	)


def parse_performance_baseline_registry(payload: object) -> PerformanceBaselineRegistry:
	if not isinstance(payload, dict):
		raise PerformanceBaselineContractError("performance baseline registry must be an object")
	_require_exact_keys(payload, ROOT_KEYS, "performance baseline registry")
	if (
		type(payload["schema_version"]) is not int
		or payload["schema_version"] != PERFORMANCE_BASELINE_SCHEMA_VERSION
	):
		raise PerformanceBaselineContractError("unsupported performance baseline schema version")
	if payload["app"] != APP_NAME:
		raise PerformanceBaselineContractError(f"performance baseline app must be {APP_NAME}")
	policy = _parse_policy(payload["policy"])
	raw_scenarios = payload["scenarios"]
	if not isinstance(raw_scenarios, list) or not 1 <= len(raw_scenarios) <= 32:
		raise PerformanceBaselineContractError("performance scenarios must be a bounded list")
	scenarios = tuple(
		_parse_scenario(scenario, index, policy) for index, scenario in enumerate(raw_scenarios)
	)
	scenario_ids = [scenario.scenario_id for scenario in scenarios]
	if len(set(scenario_ids)) != len(scenario_ids):
		raise PerformanceBaselineContractError("performance scenarios contain duplicate scenario IDs")
	canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	return PerformanceBaselineRegistry(
		schema_version=PERFORMANCE_BASELINE_SCHEMA_VERSION,
		app=APP_NAME,
		policy=policy,
		scenarios=scenarios,
		sha256=sha256(canonical.encode()).hexdigest(),
	)


def load_performance_baseline_registry(
	path: Path = PERFORMANCE_BASELINES_PATH,
) -> PerformanceBaselineRegistry:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise PerformanceBaselineContractError("cannot load performance baseline registry") from exc
	return parse_performance_baseline_registry(payload)


def parse_performance_run_summary(payload: object) -> PerformanceRunSummary:
	if not isinstance(payload, dict):
		raise PerformanceBaselineContractError("performance run summary must be an object")
	_require_exact_keys(payload, SUMMARY_KEYS, "performance run summary")
	if (
		type(payload["schema_version"]) is not int
		or payload["schema_version"] != PERFORMANCE_BASELINE_SCHEMA_VERSION
	):
		raise PerformanceBaselineContractError("unsupported performance result schema version")
	scenario_id = _require_string(payload["scenario_id"], "summary.scenario_id", max_length=64)
	if SCENARIO_ID_PATTERN.fullmatch(scenario_id) is None:
		raise PerformanceBaselineContractError("summary.scenario_id is invalid")
	scenario_version = _require_int(
		payload["scenario_version"],
		"summary.scenario_version",
		minimum=1,
		maximum=999,
	)
	profile = _require_string(payload["profile"], "summary.profile", max_length=32)
	if profile not in SUPPORTED_PROFILES:
		raise PerformanceBaselineContractError("summary.profile is unsupported")
	registry_sha256 = _require_string(
		payload["registry_sha256"],
		"summary.registry_sha256",
		max_length=64,
	)
	if SHA256_PATTERN.fullmatch(registry_sha256) is None:
		raise PerformanceBaselineContractError("summary.registry_sha256 is invalid")
	tool_version = _require_semantic_version(payload["tool_version"], "summary.tool_version")
	run_id = _require_string(payload["run_id"], "summary.run_id", max_length=64)
	if RUN_ID_PATTERN.fullmatch(run_id) is None:
		raise PerformanceBaselineContractError("summary.run_id is invalid")
	raw_metrics = payload["metrics"]
	if not isinstance(raw_metrics, dict):
		raise PerformanceBaselineContractError("summary.metrics must be an object")
	_require_exact_keys(raw_metrics, SUMMARY_METRIC_KEYS, "summary.metrics")
	metrics = PerformanceRunMetrics(
		request_count=_require_int(
			raw_metrics["request_count"],
			"summary.metrics.request_count",
			minimum=0,
			maximum=100_000,
		),
		error_rate=_require_number(
			raw_metrics["error_rate"],
			"summary.metrics.error_rate",
			minimum=0,
			maximum=1,
		),
		check_rate=_require_number(
			raw_metrics["check_rate"],
			"summary.metrics.check_rate",
			minimum=0,
			maximum=1,
		),
		requests_per_second=_require_number(
			raw_metrics["requests_per_second"],
			"summary.metrics.requests_per_second",
			minimum=0,
			maximum=1_000_000,
		),
		p95_ms=_require_number(
			raw_metrics["p95_ms"],
			"summary.metrics.p95_ms",
			minimum=0,
			maximum=3_600_000,
		),
		p99_ms=_require_number(
			raw_metrics["p99_ms"],
			"summary.metrics.p99_ms",
			minimum=0,
			maximum=3_600_000,
		),
		duration_ms=_require_number(
			raw_metrics["duration_ms"],
			"summary.metrics.duration_ms",
			minimum=0,
			maximum=3_600_000,
		),
	)
	return PerformanceRunSummary(
		schema_version=PERFORMANCE_BASELINE_SCHEMA_VERSION,
		scenario_id=scenario_id,
		scenario_version=scenario_version,
		profile=profile,
		registry_sha256=registry_sha256,
		tool_version=tool_version,
		run_id=run_id,
		metrics=metrics,
	)


def evaluate_performance_run(
	summary: PerformanceRunSummary,
	registry: PerformanceBaselineRegistry,
) -> dict[str, object]:
	violations: list[str] = []
	if summary.registry_sha256 != registry.sha256:
		violations.append("registry_sha256_mismatch")
	if summary.tool_version != registry.policy.k6_version:
		violations.append("tool_version_mismatch")
	try:
		scenario = registry.get(summary.scenario_id)
		profile = scenario.get_profile(summary.profile)
	except PerformanceBaselineContractError:
		return {
			"status": "fail",
			"passed": False,
			"violations": ["unknown_scenario_or_profile"],
		}
	if summary.scenario_version != scenario.version:
		violations.append("scenario_version_mismatch")
	metrics = summary.metrics
	thresholds = profile.thresholds
	if metrics.request_count != profile.iterations:
		violations.append("request_count")
	if metrics.error_rate > thresholds.max_error_rate:
		violations.append("error_rate")
	if metrics.check_rate < thresholds.min_check_rate:
		violations.append("check_rate")
	if metrics.p95_ms > thresholds.p95_ms:
		violations.append("p95_ms")
	if metrics.p99_ms > thresholds.p99_ms:
		violations.append("p99_ms")
	if metrics.requests_per_second < thresholds.min_requests_per_second:
		violations.append("requests_per_second")
	return {
		"status": "pass" if not violations else "fail",
		"passed": not violations,
		"scenario_id": scenario.scenario_id,
		"scenario_version": scenario.version,
		"profile": profile.profile,
		"registry_sha256": registry.sha256,
		"run_id": summary.run_id,
		"violations": violations,
		"metrics": metrics.as_dict(),
		"thresholds": thresholds.as_public_dict(),
	}


__all__ = [
	"PERFORMANCE_ALLOWED_ENVIRONMENTS",
	"PERFORMANCE_BASELINES_PATH",
	"PERFORMANCE_BASELINE_ROLES",
	"PERFORMANCE_BASELINE_SCHEMA_VERSION",
	"PerformanceBaselineContractError",
	"PerformanceBaselineRegistry",
	"PerformanceExecutionPolicy",
	"PerformanceLoadProfile",
	"PerformanceRunMetrics",
	"PerformanceRunSummary",
	"PerformanceScenarioDefinition",
	"PerformanceThresholds",
	"evaluate_performance_run",
	"load_performance_baseline_registry",
	"parse_performance_baseline_registry",
	"parse_performance_run_summary",
]
