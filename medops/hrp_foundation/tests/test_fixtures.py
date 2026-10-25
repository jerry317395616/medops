from __future__ import annotations

from typing import Any, cast

import frappe
from frappe.tests import IntegrationTestCase

from medops.api.v1.fixtures import get_fixture_governance_status as get_api_status
from medops.common.environment_profiles import load_environment_registry
from medops.common.error_catalog import IoneApplicationError
from medops.services.fixtures import (
	assert_fixture_export_allowed,
	get_fixture_governance_status,
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


class TestFixtureGovernance(IntegrationTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.original_config = {key: (key in frappe.conf, frappe.conf.get(key)) for key in CONFIG_KEYS}
		self.original_user = frappe.session.user or "Administrator"

	def tearDown(self) -> None:
		frappe.set_user(self.original_user)
		for key, (was_present, value) in self.original_config.items():
			if was_present:
				frappe.conf[key] = value
			else:
				frappe.conf.pop(key, None)
		super().tearDown()

	def _apply_profile(self, name: str) -> None:
		registry = load_environment_registry()
		frappe.conf.update(registry.get(name).expected_site_config(registry.schema_version))

	def test_system_manager_receives_redacted_read_only_status(self) -> None:
		frappe.set_user("Administrator")
		status = get_fixture_governance_status()
		repository = cast(dict[str, Any], status["repository"])
		export = cast(dict[str, Any], status["export"])
		self.assertEqual(repository["files"], 3)
		self.assertEqual(repository["records"], 0)
		self.assertFalse(export["http_write_enabled"])
		self.assertNotIn("ownership_values", frappe.as_json(status))
		self.assertNotIn("fixture_directory", frappe.as_json(status))

	def test_fixture_api_rejects_guest(self) -> None:
		frappe.set_user("Guest")
		with self.assertRaises(IoneApplicationError) as raised:
			get_api_status()
		self.assertEqual(raised.exception.code, "IONE-CORE-0001")

	def test_fixture_api_requires_system_manager_role(self) -> None:
		user_email = f"fixture-policy-{frappe.generate_hash(length=8)}@example.invalid"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": user_email,
				"first_name": "Fixture Policy User",
				"enabled": 1,
				"send_welcome_email": 0,
			}
		)
		user.insert(ignore_permissions=True)
		user.add_roles("HRP User")
		frappe.set_user(user_email)
		with self.assertRaises(IoneApplicationError) as raised:
			get_api_status()
		self.assertEqual(raised.exception.code, "IONE-CORE-0002")

	def test_export_guard_requires_managed_development_environment(self) -> None:
		self._apply_profile("test")
		with self.assertRaises(IoneApplicationError) as raised:
			assert_fixture_export_allowed()
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")
		self._apply_profile("development")
		result = assert_fixture_export_allowed()
		self.assertEqual(result["status"], "ok")
		self.assertEqual(result["environment"], "development")
