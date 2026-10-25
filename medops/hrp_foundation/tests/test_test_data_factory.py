from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.domain_service import idempotency_record_name
from medops.common.environment_profiles import load_environment_registry
from medops.common.error_catalog import IoneApplicationError
from medops.common.test_data_factory import (
	load_test_data_scenario_registry,
	synthetic_identifier,
)
from medops.services.test_data_factory import (
	GenerateTestDataService,
	TestDataRecordReference,
	generate_test_data,
	get_test_data_factory_contract_status,
)

FACTORY_METHOD = "medops.api.v1.test_data.get_test_data_factory_contract"
SCENARIO_ID = "platform-smoke"
CONFIG_KEYS = (
	"medops_environment",
	"medops_environment_schema_version",
	"medops_synthetic_data_only",
	"medops_external_integrations_enabled",
	"medops_public_access",
	"disable_email_queue",
	"developer_mode",
	"allow_tests",
)


def _idempotency_key(label: str) -> str:
	return f"COD-014-{label}-0001"


def _seed(label: str) -> str:
	return f"COD-014-seed-{label}"


def _marker_names(seed: str) -> tuple[str, ...]:
	scenario = load_test_data_scenario_registry().get(SCENARIO_ID)
	return tuple(
		synthetic_identifier(
			"ione.testdata",
			scenario_id=scenario.scenario_id,
			version=scenario.version,
			seed=seed,
			step_id=step.step_id,
		)
		for step in scenario.ordered_steps()
	)


class TestTestDataFactory(IntegrationTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.original_user = frappe.session.user or "Administrator"
		self.original_config = {key: (key in frappe.conf, frappe.conf.get(key)) for key in CONFIG_KEYS}
		registry = load_environment_registry()
		frappe.conf.update(registry.get("test").expected_site_config(registry.schema_version))
		frappe.set_user("Administrator")

	def tearDown(self) -> None:
		frappe.set_user(self.original_user)
		for key, (was_present, value) in self.original_config.items():
			if was_present:
				frappe.conf[key] = value
			else:
				frappe.conf.pop(key, None)
		super().tearDown()

	def test_generation_is_controller_backed_idempotent_and_dependency_ordered(self) -> None:
		seed = _seed("generation-0001")
		key = _idempotency_key("generation")
		first = generate_test_data(
			SCENARIO_ID,
			seed,
			idempotency_key=key,
			correlation_id=key,
		)
		replay = generate_test_data(
			SCENARIO_ID,
			seed,
			correlation_id="COD-014-generation-replay-correlation",
			idempotency_key=key,
		)
		second_key = generate_test_data(
			SCENARIO_ID,
			seed,
			idempotency_key=_idempotency_key("generation-existing"),
			correlation_id=_idempotency_key("generation-existing"),
		)

		self.assertTrue(first["result"]["changed"])
		self.assertTrue(replay["idempotency_replayed"])
		self.assertFalse(second_key["result"]["changed"])
		self.assertEqual(first["result"]["record_count"], 2)
		self.assertEqual(
			[record["name"] for record in first["result"]["records"]],
			list(_marker_names(seed)),
		)
		for marker_name in _marker_names(seed):
			document = frappe.get_doc("HRP Feature Flag", marker_name)
			self.assertFalse(document.enabled)
			self.assertEqual(document.module_name, "HRP Foundation")
			self.assertEqual(document.environment, "Test")

	def test_permission_is_checked_before_environment_and_idempotency(self) -> None:
		key = _idempotency_key("permission")
		reservation = idempotency_record_name(GenerateTestDataService.definition.name, key)
		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			patch("medops.services.test_data_factory.get_environment_status") as environment,
			self.assertRaises(IoneApplicationError) as raised,
		):
			generate_test_data(
				SCENARIO_ID,
				_seed("permission-0001"),
				idempotency_key=key,
				correlation_id=key,
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0002")
		environment.assert_not_called()
		self.assertFalse(frappe.db.exists("HRP Service Idempotency", reservation))

	def test_unmanaged_demo_and_public_environments_fail_before_writes(self) -> None:
		unsafe_statuses = (
			{"managed": False, "name": "unmanaged"},
			{
				"managed": True,
				"name": "demo",
				"allow_tests": False,
				"synthetic_data_only": True,
				"public_access": False,
				"external_integrations_enabled": False,
			},
			{
				"managed": True,
				"name": "test",
				"allow_tests": True,
				"synthetic_data_only": True,
				"public_access": True,
				"external_integrations_enabled": False,
			},
		)
		for index, status in enumerate(unsafe_statuses):
			key = _idempotency_key(f"environment-{index}")
			reservation = idempotency_record_name(GenerateTestDataService.definition.name, key)
			with (
				self.subTest(status=status),
				patch(
					"medops.services.test_data_factory.get_environment_status",
					return_value=status,
				),
				patch("medops.services.test_data_factory.frappe.get_doc") as get_doc,
				self.assertRaises(IoneApplicationError) as raised,
			):
				generate_test_data(
					SCENARIO_ID,
					_seed(f"environment-{index:04d}"),
					idempotency_key=key,
					correlation_id=key,
				)
			self.assertEqual(raised.exception.code, "IONE-CORE-0008")
			get_doc.assert_not_called()
			self.assertFalse(frappe.db.exists("HRP Service Idempotency", reservation))

	def test_partial_builder_failure_rolls_back_records_and_reservation(self) -> None:
		seed = _seed("rollback-0001")
		key = _idempotency_key("rollback")
		reservation = idempotency_record_name(GenerateTestDataService.definition.name, key)

		def failing_builder(context: Any, step: Any) -> tuple[TestDataRecordReference, ...]:
			feature_key = synthetic_identifier(
				"ione.testdata",
				scenario_id=context.scenario.scenario_id,
				version=context.scenario.version,
				seed=context.seed,
				step_id=step.step_id,
			)
			document = frappe.get_doc(
				{
					"doctype": "HRP Feature Flag",
					"feature_key": feature_key,
					"enabled": 0,
					"module_name": "HRP Foundation",
					"environment": "Test",
					"description": "Synthetic rollback marker",
				}
			)
			document.insert(ignore_permissions=True)
			if step.step_id == "dependency-marker":
				raise RuntimeError("synthetic builder failure")
			return (TestDataRecordReference("HRP Feature Flag", document.name, True),)

		with (
			patch.dict(
				"medops.services.test_data_factory._STEP_BUILDERS",
				{"feature_flag_marker": failing_builder},
			),
			self.assertRaises(IoneApplicationError) as raised,
		):
			generate_test_data(
				SCENARIO_ID,
				seed,
				idempotency_key=key,
				correlation_id=key,
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0012")
		self.assertFalse(frappe.db.exists("HRP Service Idempotency", reservation))
		for marker_name in _marker_names(seed):
			self.assertFalse(frappe.db.exists("HRP Feature Flag", marker_name))

	def test_existing_marker_conflict_does_not_overwrite_or_reserve(self) -> None:
		seed = _seed("conflict-0001")
		marker_name = _marker_names(seed)[0]
		frappe.get_doc(
			{
				"doctype": "HRP Feature Flag",
				"feature_key": marker_name,
				"enabled": 1,
				"module_name": "HRP Foundation",
				"environment": "Test",
			}
		).insert(ignore_permissions=True)
		key = _idempotency_key("conflict")
		reservation = idempotency_record_name(GenerateTestDataService.definition.name, key)
		with self.assertRaises(IoneApplicationError) as raised:
			generate_test_data(
				SCENARIO_ID,
				seed,
				idempotency_key=key,
				correlation_id=key,
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0005")
		self.assertEqual(frappe.db.get_value("HRP Feature Flag", marker_name, "enabled"), 1)
		self.assertFalse(frappe.db.exists("HRP Service Idempotency", reservation))

	def test_unknown_scenario_is_not_found_without_reservation(self) -> None:
		key = _idempotency_key("unknown")
		reservation = idempotency_record_name(GenerateTestDataService.definition.name, key)
		with self.assertRaises(IoneApplicationError) as raised:
			generate_test_data(
				"unknown-scenario",
				_seed("unknown-0001"),
				idempotency_key=key,
				correlation_id=key,
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0004")
		self.assertFalse(frappe.db.exists("HRP Service Idempotency", reservation))

	def test_builder_registry_drift_fails_before_reservation(self) -> None:
		key = _idempotency_key("builder-drift")
		reservation = idempotency_record_name(GenerateTestDataService.definition.name, key)
		with (
			patch.dict(
				"medops.services.test_data_factory._STEP_BUILDERS",
				{},
				clear=True,
			),
			self.assertRaises(IoneApplicationError) as raised,
		):
			generate_test_data(
				SCENARIO_ID,
				_seed("builder-drift-0001"),
				idempotency_key=key,
				correlation_id=key,
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0009")
		self.assertFalse(frappe.db.exists("HRP Service Idempotency", reservation))

	def test_audit_does_not_include_seed_dataset_or_record_names(self) -> None:
		seed = _seed("audit-0001")
		with patch("medops.services.audit_context.frappe.logger") as logger:
			result = generate_test_data(
				SCENARIO_ID,
				seed,
				idempotency_key=_idempotency_key("audit"),
				correlation_id=_idempotency_key("audit"),
			)
		audit_text = str(logger.mock_calls)
		self.assertNotIn(seed, audit_text)
		self.assertNotIn(result["result"]["dataset_id"], audit_text)
		for record in result["result"]["records"]:
			self.assertNotIn(record["name"], audit_text)

	def test_contract_requires_system_role_and_reports_environment(self) -> None:
		contract = cast(dict[str, Any], get_test_data_factory_contract_status())
		self.assertEqual(contract["schema_version"], 1)
		self.assertTrue(contract["generation_available"])
		self.assertEqual(contract["environment"], {"managed": True, "name": "test"})
		self.assertFalse(contract["generation_policy"]["http_write_enabled"])

		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			self.assertRaises(IoneApplicationError) as raised,
		):
			get_test_data_factory_contract_status()
		self.assertEqual(raised.exception.code, "IONE-CORE-0002")


class TestTestDataFactoryAPI(FrappeAPITestCase):
	def setUp(self) -> None:
		super().setUp()
		self.original_config = {key: (key in frappe.conf, frappe.conf.get(key)) for key in CONFIG_KEYS}
		registry = load_environment_registry()
		frappe.conf.update(registry.get("test").expected_site_config(registry.schema_version))
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def tearDown(self) -> None:
		for key, (was_present, value) in self.original_config.items():
			if was_present:
				frappe.conf[key] = value
			else:
				frappe.conf.pop(key, None)
		super().tearDown()

	def test_http_contract_is_read_only(self) -> None:
		response = self.get(self.method(FACTORY_METHOD))
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		payload = response.get_json()["message"]
		self.assertEqual(payload["schema_version"], 1)
		self.assertEqual(payload["scenario_count"], 1)
		self.assertTrue(payload["generation_available"])
		self.assertFalse(payload["generation_policy"]["http_write_enabled"])
		self.assertFalse(payload["generation_policy"]["arbitrary_doctype_input"])

	def test_http_contract_rejects_guest(self) -> None:
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		response = self.get(self.method(FACTORY_METHOD))
		self.assertEqual(response.status_code, 401, response.get_data(as_text=True))
		self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0001")
