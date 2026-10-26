"""Native diagnostic sink capture for public, non-authorizing test canaries."""

from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from unittest.mock import patch

from sentry_sdk.serializer import serialize
from sentry_sdk.utils import event_from_exception

import frappe
from frappe.core.doctype.error_log.error_log import ErrorLog

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
		self.messages = []
		self.sdk_events = []
		self.stack = ExitStack()

	def __repr__(self):
		return "<synthetic diagnostic capture>"

	def __enter__(self):
		def capture_error(doc):
			self.errors.append(frappe.as_json(doc.as_dict()))

		self.stack.enter_context(patch.object(ErrorLog, "deferred_insert", new=capture_error))
		self.stack.enter_context(patch("frappe.utils.sentry.capture_exception", new=self.capture_telemetry))
		return self

	def __exit__(self, *exception):
		return self.stack.__exit__(*exception)

	def capture_telemetry(self, **kwargs):
		self.messages.append(str(kwargs.get("message", "")))
		# Exercise the pinned SDK's real local-variable serializer without a
		# client, transport, opt-in change or outbound telemetry event.
		event, _ = event_from_exception(
			sys.exc_info(),
			client_options={
				"add_full_stack": False,
				"include_local_variables": True,
				"include_source_context": False,
				"max_value_length": 100000,
			},
		)
		request = getattr(frappe.local, "request", None)
		if request is not None:
			event["contexts"] = {
				"Request Body": request.json if request.is_json else request.form,
			}
		self.sdk_events.append(json.dumps(serialize(event, max_value_length=100000)))

	def assert_private_retry(self, test, response):
		test.assertEqual(response.status_code, 508)
		test.assertEqual(response.get_json()["exc_type"], "QueryDeadlockError")
		test.assertGreaterEqual(len(self.errors), 2)
		test.assertGreaterEqual(len(self.messages), 2)
		test.assertGreaterEqual(len(self.sdk_events), 2)
		for sink in (self.errors, self.messages, self.sdk_events, [response.get_data(as_text=True)]):
			for text in sink:
				test.assertFalse(PUBLIC_CANARY in text, "Public canary leaked into native diagnostic sink")
