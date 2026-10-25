from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import frappe

from medops.common.domain_service import DomainServiceDefinition
from medops.common.test_data_factory import (
	TEST_DATA_FACTORY_ROLES,
	TestDataFactoryContractError,
	TestDataScenarioDefinition,
	TestDataStepDefinition,
	load_test_data_scenario_registry,
	normalize_test_data_seed,
	synthetic_identifier,
	test_dataset_id,
)
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.domain_service import DomainService
from medops.services.environment import get_environment_status
from medops.services.errors import raise_ione_error, require_roles

TEST_DATA_FACTORY_SERVICE_ROLES = frozenset(TEST_DATA_FACTORY_ROLES)


@dataclass(frozen=True, slots=True)
class GenerateTestData:
	scenario_id: str
	seed: str


@dataclass(frozen=True, slots=True)
class TestDataBuildContext:
	scenario: TestDataScenarioDefinition
	seed: str
	environment: str
	dataset_id: str


@dataclass(frozen=True, slots=True)
class TestDataRecordReference:
	doctype: str
	name: str
	created: bool

	def as_dict(self) -> dict[str, object]:
		return {
			"doctype": self.doctype,
			"name": self.name,
			"created": self.created,
		}


StepBuilder = Callable[
	[TestDataBuildContext, TestDataStepDefinition],
	tuple[TestDataRecordReference, ...],
]


def _load_registry():
	try:
		registry = load_test_data_scenario_registry()
	except TestDataFactoryContractError as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)
	if any(step.builder not in _STEP_BUILDERS for scenario in registry.scenarios for step in scenario.steps):
		raise_ione_error("CONFIGURATION_INVALID")
	return registry


def _get_scenario(scenario_id: str) -> TestDataScenarioDefinition:
	registry = _load_registry()
	try:
		return registry.get(scenario_id)
	except TestDataFactoryContractError as exc:
		raise_ione_error("RESOURCE_NOT_FOUND", cause=exc)


def _is_generation_available(
	status: dict[str, object],
	scenario: TestDataScenarioDefinition,
) -> bool:
	return bool(
		status.get("managed")
		and status.get("name") in scenario.allowed_profiles
		and status.get("allow_tests")
		and status.get("synthetic_data_only")
		and not status.get("public_access")
		and not status.get("external_integrations_enabled")
	)


def _assert_generation_allowed(scenario: TestDataScenarioDefinition) -> str:
	status = get_environment_status()
	if not _is_generation_available(status, scenario):
		raise_ione_error("OPERATION_NOT_ALLOWED")
	return str(status["name"])


def _build_feature_flag_marker(
	context: TestDataBuildContext,
	step: TestDataStepDefinition,
) -> tuple[TestDataRecordReference, ...]:
	feature_key = synthetic_identifier(
		"ione.testdata",
		scenario_id=context.scenario.scenario_id,
		version=context.scenario.version,
		seed=context.seed,
		step_id=step.step_id,
	)
	expected = {
		"enabled": 0,
		"module_name": "HRP Foundation",
		"environment": context.environment.title(),
		"description": f"Synthetic marker for {context.scenario.scenario_id}/{step.step_id}",
	}
	existing = frappe.db.get_value(
		"HRP Feature Flag",
		feature_key,
		["enabled", "module_name", "environment", "description"],
		as_dict=True,
	)
	if existing:
		if any(existing.get(fieldname) != value for fieldname, value in expected.items()):
			raise_ione_error("CONFLICT")
		return (TestDataRecordReference("HRP Feature Flag", feature_key, False),)

	document = frappe.get_doc(
		{
			"doctype": "HRP Feature Flag",
			"feature_key": feature_key,
			**expected,
		}
	)
	document.insert(ignore_permissions=True)
	return (TestDataRecordReference("HRP Feature Flag", document.name, True),)


_STEP_BUILDERS: dict[str, StepBuilder] = {
	"feature_flag_marker": _build_feature_flag_marker,
}


def _execute_scenario(
	scenario: TestDataScenarioDefinition,
	seed: str,
	environment: str,
) -> dict[str, object]:
	context = TestDataBuildContext(
		scenario=scenario,
		seed=seed,
		environment=environment,
		dataset_id=test_dataset_id(scenario.scenario_id, scenario.version, seed),
	)
	records: list[TestDataRecordReference] = []
	for step in scenario.ordered_steps():
		builder = _STEP_BUILDERS.get(step.builder)
		if builder is None:
			raise_ione_error("CONFIGURATION_INVALID")
		records.extend(builder(context, step))
	changed = any(record.created for record in records)
	emit_audit_event(
		"test_data_factory_generated",
		logger_name="medops.test_data_factory",
		scenario_id=scenario.scenario_id,
		scenario_version=scenario.version,
		step_count=len(scenario.steps),
		record_count=len(records),
		changed=changed,
	)
	return {
		"status": "ok",
		"schema_version": 1,
		"scenario_id": scenario.scenario_id,
		"scenario_version": scenario.version,
		"dataset_id": context.dataset_id,
		"environment": environment,
		"step_count": len(scenario.steps),
		"record_count": len(records),
		"changed": changed,
		"records": [record.as_dict() for record in records],
	}


class GenerateTestDataService(DomainService[GenerateTestData]):
	definition = DomainServiceDefinition(
		name="hrp_foundation.test_data_factory.generate",
		version=1,
		kind="command",
		required_roles=TEST_DATA_FACTORY_SERVICE_ROLES,
	)

	def authorize(self, command: GenerateTestData) -> None:
		super().authorize(command)
		scenario = _get_scenario(command.scenario_id)
		require_roles(scenario.required_roles)

	def validate(self, command: GenerateTestData) -> None:
		normalize_test_data_seed(command.seed)
		scenario = _get_scenario(command.scenario_id)
		_assert_generation_allowed(scenario)

	def request_payload(self, command: GenerateTestData) -> dict[str, object]:
		return {
			"scenario_id": command.scenario_id,
			"seed": normalize_test_data_seed(command.seed),
		}

	def perform(self, command: GenerateTestData) -> dict[str, object]:
		scenario = _get_scenario(command.scenario_id)
		environment = _assert_generation_allowed(scenario)
		return _execute_scenario(scenario, normalize_test_data_seed(command.seed), environment)


def generate_test_data(
	scenario_id: str,
	seed: str,
	*,
	idempotency_key: object | None = None,
	correlation_id: object | None = None,
) -> dict[str, Any]:
	return (
		GenerateTestDataService()
		.execute(
			GenerateTestData(scenario_id=scenario_id, seed=seed),
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
		.as_public_dict()
	)


def get_test_data_factory_contract_status() -> dict[str, object]:
	with service_audit_scope():
		require_roles(TEST_DATA_FACTORY_SERVICE_ROLES)
		registry = _load_registry()
		status = get_environment_status()
		result = registry.as_public_dict()
		result["environment"] = {
			"managed": bool(status.get("managed")),
			"name": status.get("name"),
		}
		result["generation_available"] = any(
			_is_generation_available(status, scenario) for scenario in registry.scenarios
		)
		emit_audit_event(
			"test_data_factory_contract_read",
			logger_name="medops.test_data_factory",
			registry_sha256=registry.sha256,
			scenario_count=len(registry.scenarios),
			generation_available=result["generation_available"],
		)
		return result


__all__ = [
	"GenerateTestData",
	"GenerateTestDataService",
	"TestDataRecordReference",
	"generate_test_data",
	"get_test_data_factory_contract_status",
]
