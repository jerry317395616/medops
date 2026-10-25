from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from medops.api.v1.health import get_upstream_version_status
from medops.common.error_catalog import IoneApplicationError
from medops.setup.versions import get_version_status


class TestUpstreamVersionLock(IntegrationTestCase):
	def test_runtime_matches_resolved_lock(self) -> None:
		status = get_version_status()
		self.assertEqual(status["status"], "match", status["issues"])

	def test_version_status_rejects_guest(self) -> None:
		original_user = frappe.session.user or "Administrator"
		try:
			frappe.set_user("Guest")
			with self.assertRaises(IoneApplicationError) as raised:
				get_upstream_version_status()
			self.assertEqual(raised.exception.code, "IONE-CORE-0001")
		finally:
			frappe.set_user(original_user)
