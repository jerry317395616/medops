"""Native closed-boundary, routing and pre-authentication diagnostic regression."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests.test_api import FrappeAPITestCase
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_assets_json, get_bench_path, set_request

from medops.api.v1.finance import reservation_operations as api
from medops.common.confirmation_privacy import REDACTED_CONFIRMATION
from medops.common.error_catalog import IoneApplicationError
from medops.common.reservation_release_request import build_release_reservation_post
from medops.common.reservation_renewal_request import (
	build_renewal_command,
	build_renewed_reservation_post,
)
from medops.common.reservation_request import build_reservation_post
from medops.services.confirmation_privacy import RPC_PREFIXES, protect_confirmation_request
from medops.services.errors import raise_ione_error

PREFIX = "medops.api.v1.finance.reservation_operations."
PROOF = "synthetic_operations_proof_12345678901234567890"
HUMAN = {"confirmation": "a" * 64, "proof": PROOF, "acknowledge": True}
POST = {**HUMAN, "decision": "b" * 64}
RENEW = {
	"original_confirmation": "a" * 64,
	"original_decision": "b" * 64,
	"renewal_confirmation": "c" * 64,
	"proof": PROOF,
	"acknowledge": True,
}
CASES = (
	("post", api.PostInitialReservation, POST, "reservation_posted", build_reservation_post),
	("renew", api.RenewReservationDecision, RENEW, "decision_renewed", build_renewal_command),
	(
		"post_renewed",
		api.PostRenewedInitialReservation,
		{**RENEW, "renewal_decision": "d" * 64},
		"reservation_posted",
		build_renewed_reservation_post,
	),
	(
		"post_release",
		api.PostReservationRelease,
		HUMAN,
		"reservation_released",
		build_release_reservation_post,
	),
)
ENDPOINTS = (*(case[0] for case in CASES), "prepare_release", "inspect", "recovery")


class TestBudgetReservationOperationsBoundary(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.rollback()
		self.request_before = getattr(frappe.local, "request", None)
		self.sid_before = frappe.session.sid
		self.form_before = frappe.local.form_dict
		self.marker_before = getattr(frappe.local, "_medops_reservation_http_started", None)
		set_request(method="POST", path="/api/method/" + PREFIX + "post")
		frappe.session.sid = "synthetic-operations-test-session"
		frappe.local.form_dict = frappe._dict()
		frappe.local._medops_reservation_http_started = False

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.local.request = self.request_before
		frappe.session.sid = self.sid_before
		frappe.local.form_dict = self.form_before
		frappe.local._medops_reservation_http_started = self.marker_before
		super().tearDown()

	def test_only_strict_true_opens_master_gate_before_parsing_or_transactions(self):
		previous = frappe.conf.get("medops_reservation_operations_enabled")
		try:
			for value in (None, False, 0, 1, "true", "1"):
				frappe.conf.medops_reservation_operations_enabled = value
				for name in ENDPOINTS:
					with self.subTest(value=value, endpoint=name), self.assertRaises(IoneApplicationError):
						getattr(api, name)({"unexpected": "synthetic"})
					self.assertFalse(frappe.local._medops_reservation_http_started)
		finally:
			frappe.conf.medops_reservation_operations_enabled = previous

	def test_standard_page_sync_uses_existing_manager_roles_without_evidence_grants(self):
		page = frappe.get_doc("Page", "hrp-budget-reservations")
		self.assertEqual(page.module, "HRP Budget")
		self.assertEqual(page.standard, "Yes")
		self.assertEqual({row.role for row in page.roles}, api.MANAGER_ROLES)
		for name in (
			"frappe-web.bundle.js",
			"desk.bundle.js",
			"website.bundle.css",
			"login.bundle.css",
			"desk.bundle.css",
		):
			with self.subTest(native_asset=name):
				native_asset = get_assets_json().get(name)
				self.assertIsInstance(native_asset, str)
				self.assertTrue(native_asset.startswith("/assets/frappe/dist/"))
				file = Path(get_bench_path()) / "sites" / native_asset.lstrip("/")
				self.assertTrue(file.is_file())
				self.assertGreater(file.stat().st_size, 0)
		asset = get_assets_json().get("medops_reservation_session.bundle.js")
		self.assertIsInstance(asset, str)
		self.assertRegex(
			asset, r"^/assets/medops/dist/js/medops_reservation_session\.bundle\.[A-Za-z0-9]+\.js$"
		)
		compiled = Path(get_bench_path()) / "sites" / asset.lstrip("/")
		self.assertTrue(compiled.is_file())
		self.assertIn("reservation_result_unresolved", compiled.read_text(encoding="utf-8"))
		for doctype in (
			"HRP Budget Reservation",
			"HRP Budget Reservation Event",
			"HRP Budget Reservation Authorization",
			"HRP Budget Reservation Renewal",
			"HRP Budget Reservation Release",
			"HRP Budget Commitment Authorization",
		):
			self.assertEqual(list(frappe.get_meta(doctype).permissions), [])

	def test_token_get_background_and_missing_ack_never_start_owned_transaction(self):
		previous = frappe.conf.get("medops_reservation_operations_enabled")
		frappe.conf.medops_reservation_operations_enabled = True
		try:
			for method, headers in (("GET", {}), ("POST", {"Authorization": "Bearer synthetic"})):
				set_request(method=method, path="/", headers=headers)
				for name in ENDPOINTS:
					with self.assertRaises(IoneApplicationError):
						getattr(api, name)({})
			set_request(method="POST", path="/")
			for name, _, payload, _, _ in CASES:
				with self.assertRaises(IoneApplicationError):
					getattr(api, name)(dict(payload, acknowledge=False))
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": "reservation-nonmanager-" + uuid4().hex + "@example.invalid",
					"first_name": "Synthetic nonmanager",
					"user_type": "System User",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
			frappe.set_user(user.name)
			for name in ENDPOINTS:
				with self.assertRaises(IoneApplicationError):
					getattr(api, name)({})
			frappe.set_user("Administrator")
			frappe.local.request = None
			with self.assertRaises(IoneApplicationError):
				api.post(POST)
			self.assertFalse(frappe.local._medops_reservation_http_started)
		finally:
			frappe.set_user("Administrator")
			frappe.conf.medops_reservation_operations_enabled = previous

	def test_all_proof_methods_mask_json_form_and_rpc_aliases_before_dispatch(self):
		for name, _, payload, _, builder in CASES:
			for prefix in RPC_PREFIXES:
				for is_json in (False, True):
					with self.subTest(endpoint=name, prefix=prefix, json=is_json):
						args = {"json" if is_json else "data": {"payload": dict(payload)}}
						set_request(method="POST", path=prefix + PREFIX + name + "/compat/", **args)
						frappe.local.form_dict = frappe._dict(payload=dict(payload))
						protect_confirmation_request()
						self.assertNotIn(PROOF, repr(frappe.local.form_dict))
						diagnostic = frappe.local.request.get_json() if is_json else frappe.local.request.form
						self.assertEqual(diagnostic["payload"], REDACTED_CONFIRMATION)
						self.assertEqual(builder(frappe.local.form_dict.payload).proof, PROOF)


class TestBudgetReservationOperationsHTTP(FrappeAPITestCase):
	def test_disabled_gate_guest_get_and_unknown_fields_are_denied_over_http(self):
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)
		with patch.object(api, "_enabled", side_effect=lambda: raise_ione_error("OPERATION_NOT_ALLOWED")):
			for name in ENDPOINTS:
				response = self.post(self.method(PREFIX + name), {"payload": {}})
				self.assertEqual(response.status_code, 403, response.get_data(as_text=True))
		with patch.object(api, "_enabled"):
			for name in ENDPOINTS:
				response = self.get(self.method(PREFIX + name), {"payload": {}})
				self.assertNotEqual(response.status_code, 200)
				response = self.post(self.method(PREFIX + name), {"payload": {}, "unknown": "x"})
				self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
		self.TEST_CLIENT.delete_cookie(key="sid")
		response = self.post(self.method(PREFIX + "post"), {"payload": POST})
		self.assertEqual(response.status_code, 401, response.get_data(as_text=True))

	def test_http_dispatch_keeps_exact_private_commands_and_explicit_effects(self):
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)
		# Service substitution proves routing, not financial posting. The owned
		# native HTTP transaction still executes and preserves the request session.
		with patch.object(api, "_enabled"):
			for name, service, payload, effect, builder in CASES:
				result = SimpleNamespace(
					result={"synthetic": "result"},
					idempotency_replayed=True,
					correlation_id="synthetic-correlation",
					request_id="synthetic-request",
				)
				with patch.object(service, "execute", return_value=result) as execute:
					response = self.post(self.method(PREFIX + name), {"payload": payload})
				self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
				self.assertEqual(response.get_json()["message"]["effect"], effect)
				self.assertTrue(response.get_json()["message"]["idempotency_replayed"])
				self.assertEqual(execute.call_args.args[0], builder(payload))
				self.assertNotIn(PROOF, repr(execute.call_args.args[0]))

	def test_pre_authentication_faults_hide_all_new_proof_methods_in_native_sdk_and_logs(self):
		import frappe.app

		from medops.tests.privacy_diagnostics import PUBLIC_CANARY, NativeDiagnostics, injected_retry

		original = frappe.app.HTTPRequest

		def fail_after_session_init(*args, **kwargs):
			original(*args, **kwargs)
			injected_retry()

		for name, _, payload, _, _ in CASES:
			with (
				NativeDiagnostics() as diagnostics,
				patch.object(frappe.app, "HTTPRequest", new=fail_after_session_init),
			):
				response = self.post(
					self.method(PREFIX + name), {"payload": dict(payload, proof=PUBLIC_CANARY)}
				)
				diagnostics.assert_private_retry(self, response)
