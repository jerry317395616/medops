from __future__ import annotations

import json
import unittest

from medops.common.diagnostic_privacy import DiagnosticEvent, exception_trace, owned_method, safe_metadata


class TestDiagnosticPrivacy(unittest.TestCase):
	def test_late_request_and_local_enrichment_is_excluded_but_trace_and_frames_survive(self):
		event = DiagnosticEvent(
			event_id="a" * 32,
			level="error",
			exception={
				"values": [
					{
						"type": "QueryDeadlockError",
						"module": "frappe.exceptions",
						"value": "secret",
						"stacktrace": {
							"frames": [
								{
									"filename": "apps/medops/service.py",
									"function": "perform",
									"module": "medops.service",
									"lineno": 12,
									"vars": {"proof": "secret"},
								}
							]
						},
					}
				]
			},
			contexts={"trace": {"trace_id": "b" * 32, "span_id": "c" * 16}, "JSON Body": {"proof": "secret"}},
		)
		event.update(
			request={"headers": {"Cookie": "secret"}, "query_string": "secret"}, extra={"secret": "secret"}
		)
		wire = event.__sentry_repr__()
		self.assertNotIn("secret", json.dumps(wire))
		self.assertEqual(wire["event_id"], "a" * 32)
		self.assertEqual(wire["contexts"]["trace"]["span_id"], "c" * 16)
		frame = wire["exception"]["values"][0]["stacktrace"]["frames"][0]
		self.assertEqual((frame["lineno"], frame["function"]), (12, "perform"))
		self.assertEqual(wire["exception"]["values"][0]["type"], "QueryDeadlockError")

	def test_chained_exception_messages_and_locals_do_not_enter_trace(self):
		try:
			try:
				raise ValueError("private-payload")
			except ValueError as error:
				raise RuntimeError("private-proof") from error
		except RuntimeError as error:
			text = exception_trace(error)
		self.assertNotIn("private-payload", text)
		self.assertNotIn("private-proof", text)
		self.assertIn("ValueError", text)
		self.assertIn("RuntimeError", text)
		self.assertIn("test_chained_exception_messages", text)

	def test_metadata_excludes_body_and_identity_and_keeps_dispatch_identity(self):
		value = safe_metadata(
			{
				"type": "http_request",
				"method": "POST",
				"path": "/api/method/medops.api.confirm",
				"form_dict": {"proof": "secret"},
				"user": "private-user",
				"referrer": "private-url",
			}
		)
		self.assertEqual(value["medops_method"], "medops.api.confirm")
		self.assertNotIn("private", json.dumps(value))
		self.assertNotIn("secret", json.dumps(value))

	def test_owned_rpc_scope_handles_versions_suffix_and_legacy_cmd(self):
		for prefix in ("/api/method/", "/api/v1/method/", "/api/v2/method/"):
			self.assertEqual(owned_method(prefix + "medops.api.confirm/legacy"), "medops.api.confirm")
		self.assertEqual(owned_method("/", "medops.api.confirm"), "medops.api.confirm")
		self.assertIsNone(owned_method("/api/method/frappe.client.get"))
		self.assertIsNone(owned_method("/api/method/medops.fake?proof=secret"))

	def test_malformed_optional_sdk_contexts_still_exclude_sensitive_values(self):
		for exception, contexts in (
			(None, None),
			({"values": [None, "secret"]}, {"trace": "secret"}),
			({"values": [{"stacktrace": None, "value": "secret"}]}, {"trace": None}),
		):
			event = DiagnosticEvent(
				exception=exception, contexts=contexts, level={"proof": "secret"}, request={"proof": "secret"}
			)
			self.assertNotIn("secret", json.dumps(event.__sentry_repr__()))
		self.assertNotIn(
			"secret",
			json.dumps(
				safe_metadata({"method": ["secret"], "type": ["secret"], "queue": {"proof": "secret"}})
			),
		)
