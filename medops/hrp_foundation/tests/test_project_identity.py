from __future__ import annotations

from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from medops.common.error_catalog import IoneApplicationError
from medops.setup.install import before_install


class TestMedOpsProjectIdentity(IntegrationTestCase):
	def test_empty_ecosystem_site_is_accepted(self) -> None:
		with patch("frappe.get_installed_apps", return_value=["frappe", "erpnext", "hrms"]):
			before_install()

	def test_legacy_hrp_site_is_rejected_before_schema_changes(self) -> None:
		with (
			patch("frappe.get_installed_apps", return_value=["frappe", "ione" + "_hrp"]),
			self.assertRaises(IoneApplicationError) as raised,
		):
			before_install()
		self.assertEqual(raised.exception.definition.key, "CONFIGURATION_INVALID")
