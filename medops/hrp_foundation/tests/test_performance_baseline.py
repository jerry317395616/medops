from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.environment_profiles import load_environment_registry
from medops.common.error_catalog import IoneApplicationError
from medops.common.performance_baseline import PerformanceBaselineContractError
from medops.services.performance_baseline import get_performance_baseline_contract_status

PERFORMANCE_METHOD = "medops.api.v1.performance.get_performance_baseline_contract"
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


class PerformanceEnvironmentMixin:
	def configure_performance_environment(self) -> None:
		self.original_config = {key: (key in frappe.conf, frappe.conf.get(key)) for key in CONFIG_KEYS}
		registry = load_environment_registry()
		frappe.conf.update(registry.get("test").expected_site_config(registry.schema_version))

	def restore_performance_environment(self) -> None:
		for key, (was_present, value) in self.original_config.items():
			if was_present:
				frappe.conf[key] = value
			else:
				frappe.conf.pop(key, None)


class TestPerformanceBaseline(PerformanceEnvironmentMixin, IntegrationTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.original_user = frappe.session.user or "Administrator"
		self.configure_performance_environment()
		frappe.set_user("Administrator")

	def tearDown(self) -> None:
		frappe.set_user(self.original_user)
		self.restore_performance_environment()
		super().tearDown()

	def test_contract_is_read_only_bounded_and_available_in_test(self) -> None:
		contract = cast(dict[str, Any], get_performance_baseline_contract_status())
		self.assertEqual(contract["schema_version"], 1)
		self.assertEqual(contract["scenario_count"], 1)
		self.assertEqual(contract["environment"], {"managed": True, "name": "test"})
		self.assertTrue(contract["load_test_available"])
		self.assertFalse(contract["execution_policy"]["http_write_enabled"])
		self.assertEqual(contract["execution_policy"]["max_virtual_users"], 50)
		self.assertEqual(contract["scenarios"][0]["method"], "GET")
		self.assertTrue(contract["scenarios"][0]["read_only"])

	def test_permission_is_checked_before_registry_and_environment(self) -> None:
		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			patch("medops.services.performance_baseline.load_performance_baseline_registry") as load_registry,
			patch("medops.services.performance_baseline.get_environment_status") as environment,
			self.assertRaises(IoneApplicationError) as raised,
		):
			get_performance_baseline_contract_status()
		self.assertEqual(raised.exception.code, "IONE-CORE-0002")
		load_registry.assert_not_called()
		environment.assert_not_called()

	def test_unsafe_environments_never_report_load_test_available(self) -> None:
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
			{
				"managed": True,
				"name": "test",
				"allow_tests": True,
				"synthetic_data_only": True,
				"public_access": False,
				"external_integrations_enabled": True,
			},
		)
		for status in unsafe_statuses:
			with (
				self.subTest(status=status),
				patch(
					"medops.services.performance_baseline.get_environment_status",
					return_value=status,
				),
			):
				contract = get_performance_baseline_contract_status()
				self.assertFalse(contract["load_test_available"])

	def test_invalid_registry_maps_to_stable_configuration_error(self) -> None:
		with (
			patch(
				"medops.services.performance_baseline.load_performance_baseline_registry",
				side_effect=PerformanceBaselineContractError("local source path must not leak"),
			),
			self.assertRaises(IoneApplicationError) as raised,
		):
			get_performance_baseline_contract_status()
		self.assertEqual(raised.exception.code, "IONE-CORE-0009")
		self.assertNotIn("local source path", str(raised.exception))

	def test_repeated_reads_are_idempotent_and_do_not_create_records(self) -> None:
		before = {
			"Version": frappe.db.count("Version"),
			"Comment": frappe.db.count("Comment"),
			"Error Log": frappe.db.count("Error Log"),
			"HRP Service Idempotency": frappe.db.count("HRP Service Idempotency"),
		}
		first = get_performance_baseline_contract_status()
		second = get_performance_baseline_contract_status()
		after = {
			"Version": frappe.db.count("Version"),
			"Comment": frappe.db.count("Comment"),
			"Error Log": frappe.db.count("Error Log"),
			"HRP Service Idempotency": frappe.db.count("HRP Service Idempotency"),
		}
		self.assertEqual(first["sha256"], second["sha256"])
		self.assertEqual(first["scenarios"], second["scenarios"])
		self.assertEqual(before, after)

	def test_audit_is_redacted_and_contains_only_governed_metadata(self) -> None:
		with patch("medops.services.audit_context.frappe.logger") as logger:
			contract = get_performance_baseline_contract_status()
		audit_payload = logger.return_value.info.call_args.args[0]
		self.assertEqual(audit_payload["event"], "performance_baseline_contract_read")
		self.assertEqual(audit_payload["registry_sha256"], contract["sha256"])
		self.assertEqual(audit_payload["scenario_count"], 1)
		serialized = json.dumps(audit_payload, ensure_ascii=False)
		self.assertNotIn(frappe.local.site, serialized)
		self.assertNotIn("Administrator", serialized)
		self.assertNotIn("/api/method/", serialized)
		self.assertNotIn("target", serialized.lower())


class TestPerformanceBaselineAPI(PerformanceEnvironmentMixin, FrappeAPITestCase):
	def setUp(self) -> None:
		super().setUp()
		self.configure_performance_environment()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def tearDown(self) -> None:
		self.restore_performance_environment()
		super().tearDown()

	def test_http_contract_returns_governed_read_only_baseline(self) -> None:
		response = self.get(self.method(PERFORMANCE_METHOD))
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		payload = response.get_json()["message"]
		self.assertEqual(payload["schema_version"], 1)
		self.assertEqual(payload["scenario_count"], 1)
		self.assertTrue(payload["load_test_available"])
		self.assertFalse(payload["execution_policy"]["http_write_enabled"])
		self.assertEqual(payload["result_policy"]["independent_python_evaluation"], True)

	def test_http_contract_rejects_guest(self) -> None:
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		response = self.get(self.method(PERFORMANCE_METHOD))
		self.assertEqual(response.status_code, 401, response.get_data(as_text=True))
		self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0001")
