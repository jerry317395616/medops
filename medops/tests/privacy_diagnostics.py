"""Capture native deferred errors and actual SDK wire events, without networking."""

from __future__ import annotations

import json
import logging
from contextlib import ExitStack
from io import StringIO
from unittest.mock import patch

import sentry_sdk
from sentry_sdk.transport import Transport

import frappe
from frappe.model.document import Document
from frappe.utils.sentry import before_send
from frappe.utils.sentry import capture_exception as native_capture_exception

PUBLIC_CANARY = "diagnostic_" + "P" * 33


def injected_retry(*args, **kwargs):
	del args, kwargs
	try:
		raise frappe.QueryDeadlockError("Synthetic privacy boundary fault")
	except frappe.QueryDeadlockError:
		frappe.log_error("Synthetic query retry diagnostic", defer_insert=True)
		raise


class NativeDiagnostics:
	def __init__(self):
		self.errors = []
		self.sdk_events = []
		self.log = StringIO()
		self.stack = ExitStack()

	def __repr__(self):
		return "<synthetic diagnostic capture>"

	def __enter__(self):
		def capture_error(doc):
			# Intercept after the application's extension, at native Redis serialization.
			doc.set_user_and_timestamp()
			self.errors.append(
				frappe.as_json(doc.get_valid_dict(convert_dates_to_str=True, ignore_virtual=True))
			)

		class MemoryTransport(Transport):
			def capture_envelope(transport, envelope):
				for item in envelope.items:
					if item.headers.get("type") == "event":
						self.sdk_events.append(json.dumps(item.payload.json))

		client = sentry_sdk.Client(
			dsn="https://public@example.invalid/1",
			transport=MemoryTransport,
			default_integrations=False,
			auto_enabling_integrations=False,
			include_local_variables=True,
			include_source_context=True,
			send_default_pii=True,
			max_value_length=100000,
			before_send=before_send,
		)
		self.stack.callback(client.close)
		scope = self.stack.enter_context(sentry_sdk.isolation_scope())
		scope.set_client(client)
		original_settings = frappe.get_system_settings

		def settings(key=None):
			return 1 if key == "enable_telemetry" else original_settings(key)

		def capture_in_request_thread(*args, **kwargs):
			# Frappe's API test client starts a new Thread without copying SDK
			# ContextVars. Bind only this in-memory client in that capture scope;
			# the original native implementation and its late WSGI processor run.
			with sentry_sdk.isolation_scope() as request_scope:
				request_scope.set_client(client)
				return native_capture_exception(*args, **kwargs)

		# Only test lookup changes; no persisted opt-in change and no network transport.
		# Real native capture, scope processors, serializer and before_send still run.
		self.stack.enter_context(patch.object(frappe, "get_system_settings", new=settings))
		self.stack.enter_context(
			patch("frappe.utils.sentry.capture_exception", new=capture_in_request_thread)
		)
		self.stack.enter_context(patch.object(Document, "deferred_insert", new=capture_error))
		logger = frappe.logger()
		handler = logging.StreamHandler(self.log)
		logger.addHandler(handler)
		self.stack.callback(logger.removeHandler, handler)
		return self

	def __exit__(self, *exception):
		return self.stack.__exit__(*exception)

	def assert_private_retry(self, test, response):
		test.assertEqual(response.status_code, 508)
		test.assertEqual(response.get_json()["exc_type"], "QueryDeadlockError")
		test.assertGreaterEqual(len(self.errors), 2)
		test.assertGreaterEqual(len(self.sdk_events), 2)
		test.assertIn("QueryDeadlockError", self.log.getvalue())
		for index, sink in enumerate(
			(self.errors, self.sdk_events, [self.log.getvalue(), response.get_data(as_text=True)])
		):
			for text in sink:
				test.assertFalse(PUBLIC_CANARY in text, f"Public canary leaked into native sink {index}")
		for text in self.errors:
			doc = json.loads(text)
			test.assertTrue(doc["fingerprint"])
			test.assertIn("QueryDeadlockError", doc["error"])
			test.assertIn("File ", doc["error"])
		for text in self.sdk_events:
			event = json.loads(text)
			test.assertEqual(event["exception"]["values"][-1]["type"], "QueryDeadlockError")
			test.assertTrue(event["exception"]["values"][-1]["stacktrace"]["frames"])
