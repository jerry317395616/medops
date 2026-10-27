"""Native request-boundary and proof privacy tests; no posting is enabled."""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests.test_api import FrappeAPITestCase
from frappe.tests.utils import FrappeTestCase
from frappe.utils import set_request

from medops.api.v1.finance import reservation
from medops.common.budget_control import budget_control_key
from medops.common.budget_control_history import ReleaseInventoryEntry
from medops.common.budget_workflow import build_human_confirmation
from medops.common.confirmation_privacy import REDACTED_CONFIRMATION
from medops.common.domain_service import idempotency_record_name
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.api.reservation_transaction import prepare_reservation_http_transaction
from medops.hrp_budget.services import material_usage_preview, reservation_confirmation_service
from medops.hrp_budget.services.material_request_source import MaterialRequestSource
from medops.hrp_budget.services.reservation_confirmation_service import PrepareReservationConfirmation
from medops.services.confirmation_privacy import protect_confirmation_request
from medops.services.errors import raise_ione_error
from medops.tests import test_budget_consolidation as source_fixture
from medops.tests.test_budget_control_guards import synthetic_key


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
	def test_enabled_http_wiring_returns_exact_review_without_posting(self):
		"""Real HTTP and transaction boundary; native source and service are substituted."""
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)
		source = MaterialRequestSource(
			request="MR-SYNTHETIC",
			line="ROW-SYNTHETIC",
			request_modified="2026-01-01 00:00:00",
			line_modified="2026-01-01 00:00:00",
			company="Company",
			hospital="Hospital",
			organization_unit="Unit",
			organization_version="Version",
			mapping_revision=1,
			cost_center="Center",
			currency="CNY",
			transaction_date="2026-01-01",
			docstatus=1,
			status="Submitted",
			item_code="Item",
			uom="Unit",
			expense_account="Account",
			project=None,
			quantity="1.000000000",
			rate="10.000000000",
			amount="10.000000000",
		)

		def execute(command, **kwargs):
			del kwargs
			return SimpleNamespace(
				result={
					"confirmation": "synthetic-confirmation",
					"proof": "synthetic-proof",
					"expires_at": "2026-01-01 10:05:00.000000",
					"intent_digest": command.intent_digest,
				},
				idempotency_replayed=False,
				correlation_id="synthetic-correlation",
				request_id="synthetic-request",
			)

		with (
			patch.object(reservation, "_enabled"),
			patch.object(reservation, "locked_material_request_source", return_value=source),
			patch.object(reservation.PrepareReservationConfirmation, "execute", side_effect=execute),
		):
			response = self.post(
				self.method("medops.api.v1.finance.reservation.source"),
				{
					"payload": {
						"request": source.request,
						"line": source.line,
						"organization_unit": source.organization_unit,
					}
				},
			)
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
			captured = response.get_json()["message"]
			payload = {
				**captured,
				"allocation": "allocation",
				"budget_key": {
					"company": "Company",
					"hospital": "Hospital",
					"organization_unit": "Unit",
					"fiscal_year": "Year",
					"budget_subject": "Subject",
					"funding_source": "Fund",
					"project": None,
					"period_start": "2026-01-01",
					"period_end": "2026-12-31",
				},
				"currency": "CNY",
				"precision": 2,
				"amount": "10.00",
				"budget_date": "2026-01-01",
				"reason": "Synthetic test",
			}
			response = self.post(
				self.method("medops.api.v1.finance.reservation.prepare"), {"payload": payload}
			)
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
			review = response.get_json()["message"]
			self.assertEqual(review["effect"], "confirmation_only")
			self.assertEqual(review["review"]["source"]["request"], source.request)
			self.assertEqual(review["review"]["source"]["line"], source.line)
			self.assertEqual(review["review"]["amount"], "10.00")

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


class TestBudgetReservationDurableHTTP(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_, cls.units, _, _ = source_fixture.fixtures()
		fixture_connection = frappe.local.db
		fixture_connection.commit()

	def test_real_http_confirmation_replays_without_budget_posting(self):
		"""Committed synthetic source and evidence; budget resolution/inventory are substitutes."""
		frappe.set_user("Administrator")
		if not frappe.db.exists("Item Group", "All Item Groups"):
			frappe.get_doc(dict(doctype="Item Group", item_group_name="All Item Groups", is_group=1)).insert(
				ignore_permissions=True
			)
		if not frappe.db.exists("UOM", "COD035 HTTP Unit"):
			frappe.get_doc(dict(doctype="UOM", uom_name="COD035 HTTP Unit")).insert(ignore_permissions=True)
		item_code = "COD035-HTTP-" + uuid4().hex[:16]
		frappe.get_doc(
			dict(
				doctype="Item",
				item_code=item_code,
				item_name="Synthetic HTTP item",
				item_group="All Item Groups",
				stock_uom="COD035 HTTP Unit",
				is_stock_item=0,
			)
		).insert(ignore_permissions=True)
		center = frappe.db.get_value("HRP Organization Mapping", self.units["A"], "cost_center")
		account = frappe.db.get_value(
			"Account",
			{
				"company": source_fixture.COMPANY,
				"is_group": 0,
				"disabled": 0,
				"report_type": "Profit and Loss",
			},
			"name",
		)
		warehouse = frappe.db.get_value(
			"Warehouse", {"company": source_fixture.COMPANY, "is_group": 0}, "name"
		)
		self.assertTrue(center and account and warehouse)
		request = frappe.get_doc(
			dict(
				doctype="Material Request",
				company=source_fixture.COMPANY,
				material_request_type="Purchase",
				transaction_date="2026-10-26",
				schedule_date="2026-10-27",
				items=[
					dict(
						item_code=item_code,
						qty=1,
						uom="COD035 HTTP Unit",
						conversion_factor=1,
						warehouse=warehouse,
						cost_center=center,
						expense_account=account,
						schedule_date="2026-10-27",
					)
				],
			)
		).insert(ignore_permissions=True)
		request.items[0].rate = 10
		request.items[0].amount = 10
		request.save(ignore_permissions=True)
		request.submit()
		# The HTTP test client uses independent connections; publish only synthetic fixtures.
		fixture_connection = frappe.local.db
		fixture_connection.commit()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)
		before = {
			name: frappe.db.count(name)
			for name in ("HRP Budget Reservation", "HRP Budget Reservation Event", "HRP Budget Ledger")
		}
		values = synthetic_key(uuid4().hex)
		values.update(
			company=source_fixture.COMPANY,
			hospital=source_fixture.HOSPITAL,
			organization_unit=self.units["A"],
			project=None,
			period_start="2026-01-01",
			period_end="2026-12-31",
			budget_subject="http-subject-" + uuid4().hex[:12],
		)
		key = budget_control_key(values)
		inventory = ReleaseInventoryEntry("synthetic-release", key, "CNY", 2, "20.00", account, center)
		preview_key, confirm_key = "http-preview-" + uuid4().hex, "http-confirm-" + uuid4().hex
		with (
			patch.object(reservation, "_enabled"),
			patch.object(material_usage_preview, "resolve_budget_control_key", return_value=key),
			patch.object(material_usage_preview, "guarded_release_inventory", return_value=(inventory,)),
		):
			response = self.post(
				self.method("medops.api.v1.finance.reservation.source"),
				{
					"payload": {
						"request": request.name,
						"line": request.items[0].name,
						"organization_unit": self.units["A"],
					}
				},
			)
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
			source = response.get_json()["message"]
			payload = {
				**source,
				"allocation": "http-allocation-" + uuid4().hex[:12],
				"budget_key": asdict(key),
				"currency": "CNY",
				"precision": 2,
				"amount": "10.00",
				"budget_date": "2026-10-26",
				"reason": "Synthetic HTTP review",
			}
			url = self.method("medops.api.v1.finance.reservation.prepare")
			response = self.post(url, {"payload": payload}, headers={"Idempotency-Key": preview_key})
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
			preview = response.get_json()["message"]
			self.assertEqual(preview["effect"], "confirmation_only")
			self.assertFalse(preview["idempotency_replayed"])
			response = self.post(url, {"payload": payload}, headers={"Idempotency-Key": preview_key})
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
			replayed = response.get_json()["message"]
			self.assertTrue(replayed["idempotency_replayed"])
			self.assertEqual(
				(replayed["confirmation"], replayed["proof"]), (preview["confirmation"], preview["proof"])
			)
			changed = dict(payload, amount="9.00")
			response = self.post(url, {"payload": changed}, headers={"Idempotency-Key": preview_key})
			self.assertEqual(response.status_code, 409, response.get_data(as_text=True))

			def revoked(*args, **kwargs):
				del args, kwargs
				raise_ione_error("PERMISSION_DENIED")

			fault_key = "http-rollback-" + uuid4().hex
			fault_payload = dict(payload, allocation="http-rollback-" + uuid4().hex[:12])
			with patch.object(
				reservation_confirmation_service, "revalidate_reservation_intent", side_effect=revoked
			):
				response = self.post(url, {"payload": fault_payload}, headers={"Idempotency-Key": fault_key})
			self.assertEqual(response.status_code, 403, response.get_data(as_text=True))
			frappe.db.rollback()
			self.assertFalse(
				frappe.db.exists(
					"HRP Service Idempotency",
					idempotency_record_name(PrepareReservationConfirmation.definition.name, fault_key),
				)
			)
			self.assertEqual(
				frappe.db.count("HRP Budget Reservation Authorization", {"source_name": request.name}), 1
			)
			command = {
				"confirmation": preview["confirmation"],
				"proof": preview["proof"],
				"acknowledge": True,
			}
			url = self.method("medops.api.v1.finance.reservation.confirm")
			response = self.post(url, {"payload": command}, headers={"Idempotency-Key": confirm_key})
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
			decision = response.get_json()["message"]
			self.assertEqual(decision["effect"], "confirmation_only")
			response = self.post(url, {"payload": command}, headers={"Idempotency-Key": confirm_key})
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
			self.assertTrue(response.get_json()["message"]["idempotency_replayed"])
			self.assertEqual(response.get_json()["message"]["decision"], decision["decision"])
			response = self.post(
				url, {"payload": command}, headers={"Idempotency-Key": "duplicate-" + uuid4().hex}
			)
			self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
			previous_sid = self.sid
			new_sid = source_fixture.TestBudgetConsolidationAPI.login_actor(self, "Administrator")
			self.assertNotEqual(new_sid, previous_sid)
			response = self.post(url, {"payload": command}, headers={"Idempotency-Key": confirm_key})
			self.assertEqual(response.status_code, 403, response.get_data(as_text=True))
			self.assertNotIn(preview["proof"], response.get_data(as_text=True))
		frappe.db.rollback()
		receipt = frappe.get_doc(
			"HRP Service Idempotency",
			idempotency_record_name(PrepareReservationConfirmation.definition.name, preview_key),
		)
		self.assertNotIn(preview["proof"], receipt.response_snapshot)
		self.assertEqual({name: frappe.db.count(name) for name in before}, before)
		self.assertEqual(
			frappe.db.count("HRP Budget Reservation Authorization", {"source_name": request.name}), 2
		)
