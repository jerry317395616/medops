from __future__ import annotations

import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.audit_context import AuditContextError
from medops.common.error_catalog import IoneApplicationError
from medops.services.audit_context import (
	AUDIT_CONTEXT_JOB_KWARG,
	clear_audit_context,
	emit_audit_event,
	enqueue_with_audit,
	ensure_audit_context,
	finish_job_audit_context,
	service_audit_scope,
	start_job_audit_context,
)

METHOD = "medops.api.v1.audit.get_audit_context"
GOVERNANCE_METHOD = "medops.api.v1.change_governance.get_change_governance_status"


class TestAuditContextAPI(FrappeAPITestCase):
	def setUp(self) -> None:
		super().setUp()
		clear_audit_context()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def tearDown(self) -> None:
		clear_audit_context()
		super().tearDown()

	def test_http_context_accepts_and_echoes_a_valid_correlation_id(self) -> None:
		response = self.get(
			self.method(METHOD),
			headers={"X-Correlation-ID": "COD-010-http"},
		)

		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		payload = response.get_json()["message"]
		self.assertEqual(payload["schema_version"], 1)
		self.assertEqual(payload["correlation_id"], "COD-010-http")
		self.assertRegex(payload["request_id"], r"^req-[A-Za-z0-9]{20}$")
		self.assertIsNone(payload["parent_request_id"])
		self.assertEqual(payload["channel"], "http")
		self.assertFalse(payload["http_write_enabled"])
		self.assertEqual(response.headers["X-Correlation-ID"], payload["correlation_id"])
		self.assertEqual(response.headers["X-Request-ID"], payload["request_id"])

	def test_http_context_generates_unique_requests_under_one_correlation(self) -> None:
		first = self.get(
			self.method(METHOD),
			headers={"X-Correlation-ID": "COD-010-idempotent"},
		)
		second = self.get(
			self.method(METHOD),
			headers={"X-Correlation-ID": "COD-010-idempotent"},
		)
		first_payload = first.get_json()["message"]
		second_payload = second.get_json()["message"]

		self.assertEqual(first_payload["correlation_id"], second_payload["correlation_id"])
		self.assertNotEqual(first_payload["request_id"], second_payload["request_id"])

	def test_query_parameter_keeps_legacy_precedence_over_header(self) -> None:
		response = self.get(
			self.method(GOVERNANCE_METHOD),
			{"correlation_id": "COD-010-parameter"},
			headers={"X-Correlation-ID": "COD-010-header"},
		)

		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		payload = response.get_json()["message"]
		self.assertEqual(payload["correlation_id"], "COD-010-parameter")
		self.assertEqual(response.headers["X-Correlation-ID"], "COD-010-parameter")

	def test_invalid_header_fails_closed_without_echoing_the_input(self) -> None:
		response = self.get(
			self.method(METHOD),
			headers={"X-Correlation-ID": "patient secret"},
		)

		self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
		payload = response.get_json()["ione_error"]
		self.assertEqual(payload["code"], "IONE-CORE-0003")
		self.assertNotEqual(payload["correlation_id"], "patient secret")
		self.assertNotIn("patient secret", json.dumps(payload))
		self.assertEqual(response.headers["X-Correlation-ID"], payload["correlation_id"])
		self.assertEqual(response.headers["X-Request-ID"], payload["request_id"])

	def test_guest_is_rejected_with_a_traceable_error(self) -> None:
		self.TEST_CLIENT.delete_cookie("sid")

		response = self.get(
			self.method(METHOD),
			headers={"X-Correlation-ID": "COD-010-guest"},
		)

		self.assertEqual(response.status_code, 401, response.get_data(as_text=True))
		payload = response.get_json()["ione_error"]
		self.assertEqual(payload["code"], "IONE-CORE-0001")
		self.assertEqual(payload["correlation_id"], "COD-010-guest")
		self.assertEqual(response.headers["X-Request-ID"], payload["request_id"])

	def test_context_api_is_read_only(self) -> None:
		before = {
			"Version": frappe.db.count("Version"),
			"Comment": frappe.db.count("Comment"),
			"Error Log": frappe.db.count("Error Log"),
		}

		first = self.get(self.method(METHOD))
		second = self.get(self.method(METHOD))
		after = {
			"Version": frappe.db.count("Version"),
			"Comment": frappe.db.count("Comment"),
			"Error Log": frappe.db.count("Error Log"),
		}

		self.assertEqual(first.status_code, 200)
		self.assertEqual(second.status_code, 200)
		self.assertEqual(before, after)

	def test_enqueue_propagates_parent_and_before_job_consumes_carrier(self) -> None:
		parent = ensure_audit_context("COD-010-job")
		with patch("medops.services.audit_context.frappe.enqueue") as enqueue:
			enqueue_with_audit("medops.tests.example", queue="short", record_count=3)
		call = enqueue.call_args
		carrier = call.kwargs[AUDIT_CONTEXT_JOB_KWARG]
		self.assertEqual(carrier["correlation_id"], parent.correlation_id)
		self.assertEqual(carrier["parent_request_id"], parent.request_id)

		clear_audit_context()
		job_kwargs = {
			AUDIT_CONTEXT_JOB_KWARG: carrier,
			"record_count": 3,
		}
		child = start_job_audit_context(
			method="medops.tests.example",
			kwargs=job_kwargs,
			transaction_type="job",
		)

		self.assertNotIn(AUDIT_CONTEXT_JOB_KWARG, job_kwargs)
		self.assertEqual(child.correlation_id, parent.correlation_id)
		self.assertEqual(child.parent_request_id, parent.request_id)
		self.assertNotEqual(child.request_id, parent.request_id)
		self.assertEqual(child.channel, "job")
		finish_job_audit_context(
			method="medops.tests.example",
			kwargs=job_kwargs,
			result=None,
		)

	def test_invalid_job_carrier_is_consumed_and_replaced_safely(self) -> None:
		job_kwargs = {
			AUDIT_CONTEXT_JOB_KWARG: {
				"schema_version": 1,
				"correlation_id": "patient secret",
				"parent_request_id": "req-COD-010-parent",
			}
		}
		with patch("medops.services.audit_context.frappe.logger") as logger:
			context = start_job_audit_context(
				method="medops.tests.example",
				kwargs=job_kwargs,
				transaction_type="job",
			)

		self.assertNotIn(AUDIT_CONTEXT_JOB_KWARG, job_kwargs)
		self.assertNotEqual(context.correlation_id, "patient secret")
		payload = logger.return_value.warning.call_args.args[0]
		self.assertEqual(payload["event"], "audit_context_carrier_rejected")
		self.assertNotIn("patient", json.dumps(payload))

	def test_audit_fields_reject_sensitive_metadata_before_logging(self) -> None:
		ensure_audit_context("COD-010-redaction")
		with (
			patch("medops.services.audit_context.frappe.logger") as logger,
			self.assertRaises(AuditContextError),
		):
			emit_audit_event("unsafe_event", user_email="patient@example.invalid")
		logger.assert_not_called()

	def test_info_audit_event_reaches_a_real_file_without_changing_framework_level(self) -> None:
		logger = frappe.logger("medops.audit", allow_site=True)
		framework = frappe.logger()
		original, framework_level, global_level = logger.level, framework.level, frappe.log_level
		marker = "COD033-file-audit-" + uuid4().hex
		try:
			with TemporaryDirectory(prefix="cod033-native-audit-") as folder:
				path = Path(folder) / "event.log"
				handler = logging.FileHandler(path, encoding="utf-8")
				logger.addHandler(handler)
				try:
					logger.setLevel(logging.ERROR)
					with service_audit_scope(marker):
						payload = emit_audit_event("audit_persistence_probe", status="synthetic")
					handler.flush()
					text = path.read_text(encoding="utf-8")
					self.assertEqual(logger.level, logging.INFO)
					self.assertEqual(framework.level, framework_level)
					self.assertEqual(frappe.log_level, global_level)
					self.assertTrue(marker in text, "Normalized audit event did not reach the real file sink")
					request_id = payload["request_id"]
					self.assertTrue(
						isinstance(request_id, str) and request_id in text, "Audit request identity was lost"
					)
					self.assertTrue("audit_persistence_probe" in text, "Audit event type was lost")
					self.assertFalse("Form Dict" in text, "Request form data reached the audit sink")
				finally:
					logger.removeHandler(handler)
					handler.close()
		finally:
			logger.setLevel(original)

	def test_direct_service_scopes_are_isolated_and_nested_context_is_immutable(self) -> None:
		with service_audit_scope("COD-010-service-1") as first:
			self.assertEqual(first, ensure_audit_context("COD-010-service-1"))
			with service_audit_scope("COD-010-service-1") as nested:
				self.assertEqual(nested.request_id, first.request_id)
			with self.assertRaises(IoneApplicationError) as raised:
				with service_audit_scope("COD-010-service-replacement"):
					pass
			self.assertEqual(raised.exception.code, "IONE-CORE-0003")

		with service_audit_scope("COD-010-service-2") as second:
			self.assertEqual(second.correlation_id, "COD-010-service-2")
			self.assertNotEqual(second.request_id, first.request_id)
