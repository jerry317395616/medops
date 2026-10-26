from __future__ import annotations

import json

import sentry_sdk

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import set_request

from medops.services.diagnostic_privacy import install_diagnostic_privacy
from medops.tests.privacy_diagnostics import PUBLIC_CANARY, NativeDiagnostics


class TestDiagnosticPrivacy(IntegrationTestCase):
	def test_native_immediate_and_deferred_errors_preserve_owner_and_fingerprint(self):
		set_request(path="/api/method/medops.api.v1.finance.release.confirm")
		frappe.local.form_dict = frappe._dict(payload={"proof": PUBLIC_CANARY})
		with NativeDiagnostics() as diagnostics:
			try:
				raise frappe.QueryDeadlockError(PUBLIC_CANARY)
			except frappe.QueryDeadlockError:
				immediate = frappe.log_error(PUBLIC_CANARY, defer_insert=False)
				frappe.log_error(PUBLIC_CANARY, defer_insert=True)
			stored = frappe.get_doc("Error Log", immediate.name)
			self.assertEqual(stored.owner, frappe.session.user)
			self.assertTrue(stored.fingerprint)
			self.assertEqual(stored.method, "QueryDeadlockError")
			self.assertNotIn(PUBLIC_CANARY, frappe.as_json(stored.as_dict()))
			self.assertEqual(len(diagnostics.errors), 1)
			self.assertTrue(json.loads(diagnostics.errors[0])["fingerprint"])
			self.assertNotIn(PUBLIC_CANARY, diagnostics.errors[0])
			self.assertEqual(len(diagnostics.sdk_events), 2)
			self.assertTrue(all(PUBLIC_CANARY not in event for event in diagnostics.sdk_events))
			self.assertEqual(frappe.form_dict.payload["proof"], PUBLIC_CANARY)
			self.assertEqual(frappe.request.path, "/api/method/medops.api.v1.finance.release.confirm")

	def test_unowned_native_error_and_sdk_event_pass_through_unchanged(self):
		set_request(path="/api/method/frappe.client.get")
		frappe.local.form_dict = frappe._dict()
		install_diagnostic_privacy()
		with NativeDiagnostics() as diagnostics:
			frappe.get_doc(
				doctype="Error Log", method="Unowned fixture", error=PUBLIC_CANARY
			).deferred_insert()
			sentry_sdk.capture_event({"message": PUBLIC_CANARY, "level": "error"})
			self.assertIn(PUBLIC_CANARY, diagnostics.errors[0])
			self.assertEqual(len(diagnostics.sdk_events), 1)
			self.assertIn(PUBLIC_CANARY, diagnostics.sdk_events[0])
