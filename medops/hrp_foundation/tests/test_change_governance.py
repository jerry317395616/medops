from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.error_catalog import IoneApplicationError
from medops.services.change_governance import get_change_governance_status

METHOD = "medops.api.v1.change_governance.get_change_governance_status"


class TestChangeGovernanceAPI(FrappeAPITestCase):
	def test_http_api_returns_redacted_deterministic_status(self) -> None:
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

		response = self.get(
			self.method(METHOD),
			{"correlation_id": "COD-008-http-success"},
		)

		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		payload = response.get_json()["message"]
		self.assertEqual(payload["status"], "ok")
		self.assertEqual(payload["correlation_id"], "COD-008-http-success")
		self.assertEqual(payload["change_record_count"], 34)
		self.assertEqual(payload["decision_count"], 29)
		self.assertFalse(payload["http_write_enabled"])
		self.assertEqual(payload["write_channel"], "Git pull request only")
		serialized = json.dumps(payload, ensure_ascii=False)
		self.assertNotIn(frappe.get_app_path("medops"), serialized)
		self.assertNotIn("背景与问题", serialized)
		self.assertNotIn("产品负责人", serialized)

	def test_http_api_rejects_guest(self) -> None:
		self.TEST_CLIENT.delete_cookie("sid")

		response = self.get(self.method(METHOD))

		self.assertIn(response.status_code, {401, 403})

	def test_ordinary_hrp_user_is_rejected(self) -> None:
		with (
			patch(
				"medops.services.change_governance.frappe.get_roles",
				return_value=["HRP User"],
			),
			self.assertRaises(IoneApplicationError) as raised,
		):
			get_change_governance_status("COD-008-permission")
		self.assertEqual(raised.exception.code, "IONE-CORE-0002")

	def test_invalid_correlation_id_is_rejected(self) -> None:
		with self.assertRaises(IoneApplicationError) as raised:
			get_change_governance_status("invalid correlation id")
		self.assertEqual(raised.exception.code, "IONE-CORE-0003")

	def test_service_is_idempotent_read_only_and_audited(self) -> None:
		before = {
			"Version": frappe.db.count("Version"),
			"Comment": frappe.db.count("Comment"),
			"Error Log": frappe.db.count("Error Log"),
		}
		with patch("medops.services.audit_context.frappe.logger") as logger:
			first = get_change_governance_status("COD-008-idempotent-1")
			second = get_change_governance_status("COD-008-idempotent-2")
		after = {
			"Version": frappe.db.count("Version"),
			"Comment": frappe.db.count("Comment"),
			"Error Log": frappe.db.count("Error Log"),
		}

		self.assertEqual(first["sha256"], second["sha256"])
		self.assertEqual(before, after)
		self.assertEqual(logger.return_value.info.call_count, 2)
		audit_payload = logger.return_value.info.call_args_list[0].args[0]
		self.assertEqual(audit_payload["event"], "change_governance_status_read")
		self.assertNotIn("user", audit_payload)
		self.assertNotIn("path", json.dumps(audit_payload))
