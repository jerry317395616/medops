from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from medops.common.constants import APP_NAME
from medops.common.domain_service import DomainServiceContractError

TEST_DATA_FACTORY_SCHEMA_VERSION = 1
APP_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_DATA_SCENARIOS_PATH = APP_PACKAGE_ROOT / "config" / "test_data_scenarios.json"
TEST_DATA_FACTORY_ROLES = ("System Manager", "HRP System Manager")
TEST_DATA_ALLOWED_PROFILES = ("development", "test")
SUPPORTED_BUILDERS = ("feature_flag_marker",)
SCENARIO_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
STEP_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
SEED_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,63}$")
IDENTIFIER_PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9.]{2,31}$")
ROOT_KEYS = frozenset({"schema_version", "app", "scenarios"})
SCENARIO_KEYS = frozenset(
	{
		"scenario_id",
		"version",
		"label",
		"description",
		"allowed_profiles",
		"required_roles",
		"contains_personal_data",
		"steps",
	}
)
STEP_KEYS = frozenset({"step_id", "builder", "depends_on"})


class TestDataFactoryContractError(DomainServiceContractError):
	"""Raised when a test-data scenario or request violates the source contract."""


def _require_exact_keys(payload: dict[str, Any], expected: frozenset[str], label: str) -> None:
	actual = frozenset(payload)
	if actual != expected:
		missing = sorted(expected - actual)
		extra = sorted(actual - expected)
		raise TestDataFactoryContractError(f"{label} keys mismatch; missing={missing}, extra={extra}")


def _require_string(value: object, label: str, *, max_length: int) -> str:
	if (
		not isinstance(value, str)
		or not value
		or value != value.strip()
		or len(value) > max_length
		or any(ord(character) < 32 for character in value)
	):
		raise TestDataFactoryContractError(f"{label} is invalid")
	return value


def _require_string_list(
	value: object,
	label: str,
	*,
	allow_empty: bool = False,
	max_items: int = 32,
) -> tuple[str, ...]:
	if not isinstance(value, list) or len(value) > max_items or (not value and not allow_empty):
		raise TestDataFactoryContractError(f"{label} must be a bounded list")
	items = tuple(_require_string(item, label, max_length=140) for item in value)
	if len(set(items)) != len(items):
		raise TestDataFactoryContractError(f"{label} contains duplicates")
	return items


def normalize_test_data_seed(value: object) -> str:
	if not isinstance(value, str) or SEED_PATTERN.fullmatch(value) is None:
		raise TestDataFactoryContractError("test data seed is invalid")
	return value


def test_dataset_id(scenario_id: str, version: int, seed: str) -> str:
	normalized_seed = normalize_test_data_seed(seed)
	if SCENARIO_ID_PATTERN.fullmatch(scenario_id) is None or not 1 <= version <= 999:
		raise TestDataFactoryContractError("test data scenario identity is invalid")
	digest = sha256(f"{scenario_id}\0{version}\0{normalized_seed}".encode()).hexdigest()
	return f"tdf-{digest[:32]}"


def synthetic_identifier(
	prefix: str,
	*,
	scenario_id: str,
	version: int,
	seed: str,
	step_id: str,
) -> str:
	if IDENTIFIER_PREFIX_PATTERN.fullmatch(prefix) is None:
		raise TestDataFactoryContractError("synthetic identifier prefix is invalid")
	if STEP_ID_PATTERN.fullmatch(step_id) is None:
		raise TestDataFactoryContractError("synthetic identifier step is invalid")
	dataset = test_dataset_id(scenario_id, version, seed)
	digest = sha256(f"{dataset}\0{step_id}".encode()).hexdigest()
	return f"{prefix}.{digest[:24]}"


@dataclass(frozen=True, slots=True)
class TestDataStepDefinition:
	step_id: str
	builder: str
	depends_on: tuple[str, ...]

	def as_public_dict(self) -> dict[str, object]:
		return {
			"step_id": self.step_id,
			"depends_on": list(self.depends_on),
		}


@dataclass(frozen=True, slots=True)
class TestDataScenarioDefinition:
	scenario_id: str
	version: int
	label: str
	description: str
	allowed_profiles: tuple[str, ...]
	required_roles: tuple[str, ...]
	contains_personal_data: bool
	steps: tuple[TestDataStepDefinition, ...]

	def ordered_steps(self) -> tuple[TestDataStepDefinition, ...]:
		by_id = {step.step_id: step for step in self.steps}
		ordered: list[TestDataStepDefinition] = []
		pending = list(self.steps)
		completed: set[str] = set()
		while pending:
			progress = False
			for step in tuple(pending):
				if set(step.depends_on).issubset(completed):
					ordered.append(step)
					completed.add(step.step_id)
					pending.remove(step)
					progress = True
			if not progress:
				raise TestDataFactoryContractError(f"{self.scenario_id} contains a cyclic step dependency")
		if set(by_id) != completed:
			raise TestDataFactoryContractError(f"{self.scenario_id} contains an unresolved step dependency")
		return tuple(ordered)

	def as_public_dict(self) -> dict[str, object]:
		return {
			"scenario_id": self.scenario_id,
			"version": self.version,
			"label": self.label,
			"description": self.description,
			"allowed_profiles": list(self.allowed_profiles),
			"required_roles": list(self.required_roles),
			"contains_personal_data": self.contains_personal_data,
			"step_count": len(self.steps),
			"steps": [step.as_public_dict() for step in self.ordered_steps()],
		}


@dataclass(frozen=True, slots=True)
class TestDataScenarioRegistry:
	schema_version: int
	app: str
	scenarios: tuple[TestDataScenarioDefinition, ...]
	sha256: str

	def get(self, scenario_id: str) -> TestDataScenarioDefinition:
		for scenario in self.scenarios:
			if scenario.scenario_id == scenario_id:
				return scenario
		raise TestDataFactoryContractError(f"unknown test data scenario: {scenario_id}")

	def as_public_dict(self) -> dict[str, object]:
		return {
			"status": "ok",
			"schema_version": self.schema_version,
			"app": self.app,
			"sha256": self.sha256,
			"scenario_count": len(self.scenarios),
			"scenarios": [scenario.as_public_dict() for scenario in self.scenarios],
			"seed_policy": {
				"minimum_length": 8,
				"maximum_length": 64,
				"deterministic": True,
				"stored_in_audit": False,
			},
			"generation_policy": {
				"managed_environment_required": True,
				"allow_tests_required": True,
				"synthetic_data_only": True,
				"arbitrary_doctype_input": False,
				"contains_personal_data": False,
				"http_write_enabled": False,
				"cleanup": "replaceable_environment_reset",
			},
		}


def _parse_step(payload: object, scenario_id: str, index: int) -> TestDataStepDefinition:
	label = f"{scenario_id}.steps[{index}]"
	if not isinstance(payload, dict):
		raise TestDataFactoryContractError(f"{label} must be an object")
	_require_exact_keys(payload, STEP_KEYS, label)
	step_id = _require_string(payload["step_id"], f"{label}.step_id", max_length=64)
	if STEP_ID_PATTERN.fullmatch(step_id) is None:
		raise TestDataFactoryContractError(f"{label}.step_id is invalid")
	builder = _require_string(payload["builder"], f"{label}.builder", max_length=64)
	if builder not in SUPPORTED_BUILDERS:
		raise TestDataFactoryContractError(f"{label}.builder is unsupported")
	return TestDataStepDefinition(
		step_id=step_id,
		builder=builder,
		depends_on=_require_string_list(
			payload["depends_on"],
			f"{label}.depends_on",
			allow_empty=True,
		),
	)


def _parse_scenario(payload: object, index: int) -> TestDataScenarioDefinition:
	label = f"scenarios[{index}]"
	if not isinstance(payload, dict):
		raise TestDataFactoryContractError(f"{label} must be an object")
	_require_exact_keys(payload, SCENARIO_KEYS, label)
	scenario_id = _require_string(payload["scenario_id"], f"{label}.scenario_id", max_length=64)
	if SCENARIO_ID_PATTERN.fullmatch(scenario_id) is None:
		raise TestDataFactoryContractError(f"{label}.scenario_id is invalid")
	version = payload["version"]
	if type(version) is not int or not 1 <= version <= 999:
		raise TestDataFactoryContractError(f"{label}.version is invalid")
	allowed_profiles = _require_string_list(
		payload["allowed_profiles"],
		f"{label}.allowed_profiles",
	)
	if any(profile not in TEST_DATA_ALLOWED_PROFILES for profile in allowed_profiles):
		raise TestDataFactoryContractError(f"{label}.allowed_profiles is unsafe")
	required_roles = _require_string_list(payload["required_roles"], f"{label}.required_roles")
	if any(role not in TEST_DATA_FACTORY_ROLES for role in required_roles):
		raise TestDataFactoryContractError(f"{label}.required_roles is unsafe")
	if payload["contains_personal_data"] is not False:
		raise TestDataFactoryContractError(f"{label} must forbid personal data")
	raw_steps = payload["steps"]
	if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= 32:
		raise TestDataFactoryContractError(f"{label}.steps must be a bounded list")
	steps = tuple(_parse_step(step, scenario_id, step_index) for step_index, step in enumerate(raw_steps))
	step_ids = {step.step_id for step in steps}
	if len(step_ids) != len(steps):
		raise TestDataFactoryContractError(f"{label}.steps contains duplicate step IDs")
	if any(dependency not in step_ids for step in steps for dependency in step.depends_on):
		raise TestDataFactoryContractError(f"{label}.steps contains an unknown dependency")
	scenario = TestDataScenarioDefinition(
		scenario_id=scenario_id,
		version=version,
		label=_require_string(payload["label"], f"{label}.label", max_length=140),
		description=_require_string(
			payload["description"],
			f"{label}.description",
			max_length=500,
		),
		allowed_profiles=allowed_profiles,
		required_roles=required_roles,
		contains_personal_data=False,
		steps=steps,
	)
	scenario.ordered_steps()
	return scenario


def parse_test_data_scenario_registry(payload: object) -> TestDataScenarioRegistry:
	if not isinstance(payload, dict):
		raise TestDataFactoryContractError("test data scenario registry must be an object")
	_require_exact_keys(payload, ROOT_KEYS, "test data scenario registry")
	if (
		type(payload["schema_version"]) is not int
		or payload["schema_version"] != TEST_DATA_FACTORY_SCHEMA_VERSION
	):
		raise TestDataFactoryContractError("unsupported test data scenario schema version")
	if payload["app"] != APP_NAME:
		raise TestDataFactoryContractError(f"test data scenario app must be {APP_NAME}")
	raw_scenarios = payload["scenarios"]
	if not isinstance(raw_scenarios, list) or not 1 <= len(raw_scenarios) <= 64:
		raise TestDataFactoryContractError("test data scenarios must be a bounded list")
	scenarios = tuple(_parse_scenario(scenario, index) for index, scenario in enumerate(raw_scenarios))
	scenario_ids = [scenario.scenario_id for scenario in scenarios]
	if len(set(scenario_ids)) != len(scenario_ids):
		raise TestDataFactoryContractError("test data scenarios contain duplicate scenario IDs")
	canonical = json.dumps(
		payload,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
	)
	return TestDataScenarioRegistry(
		schema_version=TEST_DATA_FACTORY_SCHEMA_VERSION,
		app=APP_NAME,
		scenarios=scenarios,
		sha256=sha256(canonical.encode()).hexdigest(),
	)


def load_test_data_scenario_registry(
	path: Path = TEST_DATA_SCENARIOS_PATH,
) -> TestDataScenarioRegistry:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise TestDataFactoryContractError("cannot load test data scenarios") from exc
	return parse_test_data_scenario_registry(payload)


__all__ = [
	"TEST_DATA_ALLOWED_PROFILES",
	"TEST_DATA_FACTORY_ROLES",
	"TEST_DATA_FACTORY_SCHEMA_VERSION",
	"TestDataFactoryContractError",
	"TestDataScenarioDefinition",
	"TestDataScenarioRegistry",
	"TestDataStepDefinition",
	"load_test_data_scenario_registry",
	"normalize_test_data_seed",
	"parse_test_data_scenario_registry",
	"synthetic_identifier",
	"test_dataset_id",
]
