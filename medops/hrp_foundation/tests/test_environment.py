from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from medops.api.v1.environment import get_environment_status as get_api_status
from medops.common.environment_profiles import load_environment_registry
from medops.common.error_catalog import IoneApplicationError
from medops.services.environment import (
	assert_external_integrations_allowed,
	get_environment_status,
)

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


class TestEnvironmentStatus(IntegrationTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.original_config = {key: (key in frappe.conf, frappe.conf.get(key)) for key in CONFIG_KEYS}

	def tearDown(self) -> None:
		for key, (was_present, value) in self.original_config.items():
			if was_present:
				frappe.conf[key] = value
			else:
				frappe.conf.pop(key, None)
		super().tearDown()

	def _apply_profile(self, name: str) -> None:
		registry = load_environment_registry()
		frappe.conf.update(registry.get(name).expected_site_config(registry.schema_version))

	def test_unmanaged_site_is_reported_without_guessing_policy(self) -> None:
		for key in CONFIG_KEYS:
			frappe.conf.pop(key, None)
		self.assertEqual(
			get_environment_status(),
			{"managed": False, "name": "unmanaged", "schema_version": None},
		)

	def test_managed_status_is_read_only_and_redacted(self) -> None:
		self._apply_profile("test")
		status = get_environment_status()
		self.assertTrue(status["managed"])
		self.assertEqual(status["name"], "test")
		self.assertNotIn("bench_dir", status)
		self.assertNotIn("ports", status)
		self.assertTrue(status["synthetic_data_only"])
		self.assertFalse(status["external_integrations_enabled"])

	def test_configuration_drift_fails_closed(self) -> None:
		self._apply_profile("demo")
		frappe.conf["allow_tests"] = True
		with self.assertRaises(IoneApplicationError) as raised:
			get_environment_status()
		self.assertEqual(raised.exception.code, "IONE-CORE-0009")

	def test_external_integrations_are_denied(self) -> None:
		self._apply_profile("development")
		with self.assertRaises(IoneApplicationError) as raised:
			assert_external_integrations_allowed()
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")

	def test_environment_api_rejects_guest(self) -> None:
		self._apply_profile("test")
		original_user = frappe.session.user or "Administrator"
		try:
			frappe.set_user("Guest")
			with self.assertRaises(IoneApplicationError) as raised:
				get_api_status()
			self.assertEqual(raised.exception.code, "IONE-CORE-0001")
		finally:
			frappe.set_user(original_user)
