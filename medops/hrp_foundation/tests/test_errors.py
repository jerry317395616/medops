from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.error_catalog import ErrorCatalogError, IoneApplicationError
from medops.services.errors import (
	get_error_catalog_status,
	raise_ione_error,
)

CATALOG_METHOD = "medops.api.v1.errors.get_error_catalog"
GOVERNANCE_METHOD = "medops.api.v1.change_governance.get_change_governance_status"


class TestIoneErrors(FrappeAPITestCase):
	def setUp(self) -> None:
		super().setUp()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def test_http_catalog_is_admin_only_read_only_and_deterministic(self) -> None:
		first = self.get(self.method(CATALOG_METHOD))
		second = self.get(self.method(CATALOG_METHOD))

		self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
		self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
		first_payload = first.get_json()["message"]
		second_payload = second.get_json()["message"]
		self.assertEqual(first_payload, second_payload)
		self.assertEqual(first_payload["namespace"], "IONE-CORE")
		self.assertEqual(first_payload["error_count"], 12)
		self.assertFalse(first_payload["http_write_enabled"])
		self.assertNotIn("log_level", json.dumps(first_payload))
		self.assertNotIn('"key"', json.dumps(first_payload))

	def test_guest_receives_machine_error_status_headers_and_chinese_message(self) -> None:
		self.TEST_CLIENT.delete_cookie("sid")

		response = self.get(self.method(CATALOG_METHOD), {"_lang": "zh"})

		self.assertEqual(response.status_code, 401, response.get_data(as_text=True))
		payload = response.get_json()["ione_error"]
		self.assertEqual(payload["code"], "IONE-CORE-0001")
		self.assertEqual(payload["category"], "authentication")
		self.assertEqual(payload["message"], "需要先登录。")
		self.assertFalse(payload["retryable"])
		self.assertEqual(payload["correlation_id"], response.headers["X-Correlation-ID"])
		self.assertEqual(payload["request_id"], response.headers["X-Request-ID"])
		self.assertEqual(response.headers["X-Ione-Error-Code"], payload["code"])
		self.assertEqual(response.headers["X-Ione-Error-ID"], payload["error_id"])

	def test_authenticated_non_manager_receives_permission_error(self) -> None:
		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			self.assertRaises(IoneApplicationError) as raised,
		):
			get_error_catalog_status()

		self.assertEqual(raised.exception.code, "IONE-CORE-0002")
		self.assertEqual(raised.exception.http_status_code, 403)

	def test_existing_api_uses_standard_validation_envelope(self) -> None:
		response = self.get(
			self.method(GOVERNANCE_METHOD),
			{"correlation_id": "invalid correlation id", "_lang": "zh"},
		)

		self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
		payload = response.get_json()["ione_error"]
		self.assertEqual(payload["code"], "IONE-CORE-0003")
		self.assertEqual(payload["category"], "validation")
		self.assertEqual(payload["message"], "请求参数无效。")
		self.assertEqual(payload["correlation_id"], response.headers["X-Correlation-ID"])
		self.assertEqual(payload["request_id"], response.headers["X-Request-ID"])

	def test_audit_is_redacted_and_unknown_key_fails_closed(self) -> None:
		sensitive = RuntimeError("patient=secret token=super-secret /private/path")
		with (
			patch("medops.services.audit_context.frappe.logger") as logger,
			self.assertRaises(IoneApplicationError) as raised,
		):
			raise_ione_error("UNKNOWN_ERROR_KEY", cause=sensitive)

		self.assertEqual(raised.exception.code, "IONE-CORE-0012")
		payload = logger.return_value.error.call_args.args[0]
		serialized = json.dumps(payload)
		self.assertEqual(payload["event"], "ione_error_raised")
		self.assertEqual(payload["cause_type"], "ErrorCatalogError")
		self.assertNotIn("patient", serialized)
		self.assertNotIn("secret", serialized)
		self.assertNotIn("path", serialized)
		self.assertNotIn("user", payload)
		self.assertNotIn("message", payload)

	def test_corrupt_catalog_uses_locked_internal_fallback(self) -> None:
		with (
			patch(
				"medops.services.errors.load_error_catalog",
				side_effect=ErrorCatalogError("patient=secret /private/path"),
			),
			patch("medops.services.audit_context.frappe.logger") as logger,
			self.assertRaises(IoneApplicationError) as raised,
		):
			raise_ione_error("INVALID_REQUEST")

		self.assertEqual(raised.exception.code, "IONE-CORE-0012")
		self.assertEqual(raised.exception.http_status_code, 500)
		payload = logger.return_value.error.call_args.args[0]
		self.assertEqual(payload["cause_type"], "ErrorCatalogError")
		self.assertNotIn("secret", json.dumps(payload))
		self.assertNotIn("path", json.dumps(payload))

	def test_invalid_translation_fails_as_configuration_error(self) -> None:
		with (
			patch(
				"medops.services.errors.validate_error_translations",
				side_effect=ErrorCatalogError("invalid translation"),
			),
			self.assertRaises(IoneApplicationError) as raised,
		):
			get_error_catalog_status()

		self.assertEqual(raised.exception.code, "IONE-CORE-0009")
		self.assertEqual(raised.exception.http_status_code, 500)

	def test_catalog_service_is_idempotent_and_does_not_write(self) -> None:
		before = {
			"Version": frappe.db.count("Version"),
			"Comment": frappe.db.count("Comment"),
			"Error Log": frappe.db.count("Error Log"),
		}
		with patch("medops.services.audit_context.frappe.logger") as logger:
			first = get_error_catalog_status()
			second = get_error_catalog_status()
		after = {
			"Version": frappe.db.count("Version"),
			"Comment": frappe.db.count("Comment"),
			"Error Log": frappe.db.count("Error Log"),
		}

		self.assertEqual(first["sha256"], second["sha256"])
		self.assertEqual(before, after)
		self.assertEqual(logger.return_value.info.call_count, 2)
