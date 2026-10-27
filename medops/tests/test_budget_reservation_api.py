"""Native request-boundary and proof privacy tests; no posting is enabled."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.test_api import FrappeAPITestCase
from frappe.tests.utils import FrappeTestCase
from frappe.utils import set_request

from medops.api.v1.finance import reservation
from medops.common.budget_workflow import build_human_confirmation
from medops.common.confirmation_privacy import REDACTED_CONFIRMATION
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.api.reservation_transaction import prepare_reservation_http_transaction
from medops.services.confirmation_privacy import protect_confirmation_request


class TestBudgetReservationAPI(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.rollback()
		self.request_before = getattr(frappe.local, "request", None)
		self.sid_before = frappe.session.sid
		self.form_before = frappe.local.form_dict
		self.marker_before = getattr(frappe.local, "_medops_reservation_http_started", None)
		set_request(method="POST", path="/api/method/medops.api.v1.finance.reservation.prepare")
		frappe.session.sid = "synthetic-test-web-session"
		frappe.local.form_dict = frappe._dict(payload="synthetic")
		frappe.local._medops_reservation_http_started = False

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.local.request = self.request_before
		frappe.session.sid = self.sid_before
		frappe.local.form_dict = self.form_before
		frappe.local._medops_reservation_http_started = self.marker_before
		super().tearDown()

	def test_clean_request_uses_read_committed_without_resetting_session_or_form(self):
		form = frappe.local.form_dict
		prepare_reservation_http_transaction()
		self.assertEqual(frappe.session.sid, "synthetic-test-web-session")
		self.assertIs(frappe.local.form_dict, form)
		self.assertEqual(str(frappe.db.sql("SELECT @@tx_isolation")[0][0]).upper(), "READ-COMMITTED")
		with self.assertRaises(IoneApplicationError):
			prepare_reservation_http_transaction()

	def test_pending_write_is_not_rolled_back(self):
		frappe.db.sql("CREATE TEMPORARY TABLE `cod035_http_pending` (`name` VARCHAR(20)) ENGINE=InnoDB")
		try:
			frappe.db.sql("INSERT INTO `cod035_http_pending` (`name`) VALUES (%s)", ("synthetic",))
			with self.assertRaises(IoneApplicationError):
				prepare_reservation_http_transaction()
			self.assertEqual(frappe.db.sql("SELECT COUNT(*) FROM `cod035_http_pending`")[0][0], 1)
		finally:
			frappe.db.rollback()
			frappe.db.sql("DROP TEMPORARY TABLE `cod035_http_pending`")

	def test_pending_callback_is_not_discarded(self):
		def callback():
			return None

		frappe.db.after_commit.add(callback)
		with self.assertRaises(IoneApplicationError):
			prepare_reservation_http_transaction()
		self.assertIn(callback, frappe.db.after_commit._functions)
		frappe.db.after_commit.reset()

	def test_feature_is_disabled_by_default(self):
		previous = frappe.conf.get("medops_reservation_confirmation_enabled")
		try:
			frappe.conf.medops_reservation_confirmation_enabled = False
			with self.assertRaises(IoneApplicationError):
				reservation.source({"request": "x", "line": "y", "organization_unit": "z"})
			self.assertFalse(frappe.local._medops_reservation_http_started)
		finally:
			frappe.conf.medops_reservation_confirmation_enabled = previous

	def test_reservation_confirmation_body_is_masked_before_dispatch(self):
		proof = "synthetic_confirmation_proof_1234567890"
		payload = {"confirmation": "synthetic", "proof": proof, "acknowledge": True}
		set_request(
			method="POST",
			path="/api/method/medops.api.v1.finance.reservation.confirm",
			json={"payload": payload},
		)
		frappe.local.form_dict = frappe._dict(payload=payload.copy())
		protect_confirmation_request()
		self.assertNotIn(proof, repr(frappe.local.form_dict))
		self.assertEqual(frappe.local.request.get_json()["payload"], REDACTED_CONFIRMATION)
		self.assertEqual(build_human_confirmation(frappe.local.form_dict.payload).proof, proof)


class TestBudgetReservationHTTP(FrappeAPITestCase):
	def test_disabled_confirmation_rejects_a_real_http_request(self):
		if frappe.conf.get("medops_reservation_confirmation_enabled") is True:
			self.skipTest("This case checks the default-disabled configuration")
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)
		proof = "synthetic_confirmation_proof_1234567890"
		before = frappe.db.count("HRP Budget Reservation Authorization")
		response = self.post(
			self.method("medops.api.v1.finance.reservation.confirm"),
			{"payload": {"confirmation": "synthetic", "proof": proof, "acknowledge": True}},
		)
		self.assertNotEqual(response.status_code, 200)
		self.assertNotIn(proof, response.get_data(as_text=True))
		frappe.db.rollback()
		self.assertEqual(frappe.db.count("HRP Budget Reservation Authorization"), before)

	def test_confirmation_pre_hook_fault_hides_proof_in_native_diagnostics(self):
		import frappe.app

		from medops.tests.privacy_diagnostics import PUBLIC_CANARY, NativeDiagnostics, injected_retry

		original = frappe.app.HTTPRequest

		def fail_after_session_init(*args, **kwargs):
			original(*args, **kwargs)
			injected_retry()

		with (
			NativeDiagnostics() as diagnostics,
			patch.object(frappe.app, "HTTPRequest", new=fail_after_session_init),
		):
			response = self.post(
				self.method("medops.api.v1.finance.reservation.confirm"),
				{"payload": {"confirmation": "synthetic", "proof": PUBLIC_CANARY, "acknowledge": True}},
			)
			diagnostics.assert_private_retry(self, response)
