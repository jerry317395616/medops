from __future__ import annotations

import json
from contextlib import nullcontext
from unittest.mock import patch

import frappe
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.budget_ledger import LEDGER, RELEASE, RELEASE_ENTRY
from medops.common.business_rules import rule_payload
from medops.common.integration_outbox import OUTBOX
from medops.hrp_budget.services.release_evidence import release_authorization
from medops.hrp_workflow_authorization.services.business_rules import UpsertBusinessRule
from medops.tests import test_budget_consolidation as source_fixture
from medops.tests.scope_fixtures import share_test_records
from medops.tests.test_budget_release_preparation import release_rule_fixtures
from medops.tests.test_budget_submission import fixture_request


class TestBudgetReleaseConfirmation(FrappeAPITestCase):
	login_actor = source_fixture.TestBudgetConsolidationAPI.login_actor
	success = source_fixture.TestBudgetConsolidationAPI.success
	post_command = source_fixture.TestBudgetConsolidationAPI.post_command
	request_approval = source_fixture.TestBudgetConsolidationAPI.request_approval
	decide = source_fixture.TestBudgetConsolidationAPI.decide
	new_sources = source_fixture.TestBudgetConsolidationAPI.new_sources
	proposal = source_fixture.TestBudgetConsolidationAPI.proposal
	seal = source_fixture.TestBudgetConsolidationAPI.seal

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.org, cls.units, cls.value, cls.fund = source_fixture.fixtures()
		release_rule_fixtures()

	def setUp(self):
		super().setUp()
		self.new_release()

	def new_release(self):
		self.sources = self.new_sources()
		name = self.success(self.seal())["name"]
		case = self.request_approval(name)
		self.login_actor(source_fixture.REVIEWER)
		self.success(self.decide(case))
		self.login_actor(source_fixture.MANAGER)
		self.release = self.success(
			self.post_command(
				"release.create",
				dict(
					approval_case=case,
					expected_approval_revision=2,
					posting_date="2026-10-26",
					reason="合成财务下达确认验收",
				),
			)
		)["name"]

	def preview(self, revision=1):
		return self.post_command("release.prepare", dict(release=self.release, expected_revision=revision))

	def confirm(self, preview, *, key=None, allow_dispatch=False, **overrides):
		# Ordinary synthetic authorization tests do not leave real financial jobs
		# in Redis. Dedicated dispatch tests opt in to production callbacks.
		with nullcontext() if allow_dispatch else patch("medops.hrp_budget.jobs.dispatch._enqueue_committed"):
			return self.post_command(
				"release.confirm",
				dict(
					confirmation=preview["confirmation"],
					proof=preview["proof"],
					acknowledge=True,
				)
				| overrides,
				request_key=key,
			)

	def assert_unposted(self):
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 0)
		self.assertEqual(frappe.db.get_value(RELEASE, self.release, "docstatus"), 0)

	def test_cookie_confirmation_persists_one_authority_and_message_without_posting(self):
		before = {doctype: frappe.db.count(doctype) for doctype in (LEDGER, "GL Entry", "Stock Ledger Entry")}
		preview = self.success(self.preview())
		self.assertEqual(preview["total_amount"], "100000.000000001")
		key = fixture_request("financial-confirm")
		first = self.success(self.confirm(preview, key=key))
		self.assertEqual(first["status"], "Authorized")
		self.assertEqual(first["revision"], 2)
		self.assertTrue(self.success(self.confirm(preview, key=key))["idempotency_replayed"])
		self.assertEqual(self.confirm(preview).status_code, 409)
		doc = frappe.get_doc(RELEASE, self.release)
		decision = release_authorization(doc)
		self.assertEqual(decision.name, first["decision"])
		self.assertEqual(decision.actor, source_fixture.MANAGER)
		self.assertEqual(frappe.db.count(RELEASE_ENTRY, {"release": self.release}), 2)
		self.assertEqual(frappe.db.count(OUTBOX, {"reference_name": self.release}), 1)
		self.assertEqual(frappe.db.get_value(OUTBOX, doc.outbox_message, "status"), "Pending")
		self.assertEqual(before, {doctype: frappe.db.count(doctype) for doctype in before})
		public = self.success(
			self.get(self.method(source_fixture.PREFIX + "release.get_record"), {"name": self.release})
		)
		self.assertEqual(public["total_amount"], "100000.000000001")
		self.assertFalse(
			{
				"outbox_message",
				"intent_digest",
				"source_snapshot_digest",
				"session_digest",
				"confirmation_hash",
			}.intersection(public)
		)
		self.assert_unposted()

	def test_native_retry_diagnostics_hide_confirmation_body_and_preserve_508(self):
		from frappe.core.doctype.error_log.error_log import ErrorLog
		from frappe.tests.test_api import make_request
		from frappe.utils.logger import sanitized_dict

		# The canary is deliberately not a valid persisted proof for any action.
		preview = dict(confirmation="synthetic-confirmation-diagnostic", proof="diagnostic_" + "a" * 33)
		captured = []
		telemetry = []

		def save_diagnostic(doc):
			captured.append(frappe.as_json(doc.as_dict()))

		def fail_after_validation(command, **kwargs):
			del kwargs
			# Exercise the native database diagnostic path before the exception
			# unwinds, then the ordinary WSGI 508 error-snapshot path as well.
			self.assertTrue(command.proof == preview["proof"], "Opaque RPC body lost proof bytes")
			captured.append(frappe.as_json(sanitized_dict(frappe.form_dict)))
			telemetry.append(
				frappe.as_json(frappe.request.json if frappe.request.is_json else frappe.request.form)
			)
			try:
				raise frappe.QueryDeadlockError("Synthetic confirmation retry diagnostic")
			except frappe.QueryDeadlockError:
				frappe.log_error("Query deadlocked", defer_insert=True)
				raise

		for endpoint, transport in (("release", "json"), ("release", "form"), ("approval", "json")):
			with self.subTest(endpoint=endpoint, transport=transport):
				captured.clear()
				telemetry.clear()
				payload = dict(confirmation=preview["confirmation"], proof=preview["proof"], acknowledge=True)
				options = (
					{"json": {"payload": payload}}
					if transport == "json"
					else {"data": {"payload": json.dumps(payload)}}
				)
				with (
					patch.object(ErrorLog, "deferred_insert", new=save_diagnostic),
					patch(
						"frappe.utils.sentry.capture_exception", side_effect=lambda **kw: telemetry.append(kw)
					),
					patch(
						"medops.hrp_budget.services.release_confirmation.confirm_release_authorization"
						if endpoint == "release"
						else "medops.hrp_budget.services.approval.confirm_decision",
						side_effect=fail_after_validation,
					),
				):
					response = make_request(
						target=self.TEST_CLIENT.post,
						args=(self.method(source_fixture.PREFIX + endpoint + ".confirm"),),
						kwargs={
							"headers": {"Idempotency-Key": fixture_request("confirmation-privacy")},
							**options,
						},
					)
				self.assertEqual(response.status_code, 508)
				self.assertEqual(response.get_json()["exc_type"], "QueryDeadlockError")
				self.assertGreaterEqual(len(captured), 3)
				self.assertGreaterEqual(len(telemetry), 3)
				for text in (*captured, frappe.as_json(telemetry), response.get_data(as_text=True)):
					self.assertFalse(preview["proof"] in text, "Confirmation proof leaked into diagnostic")
				self.assertTrue(any("QueryDeadlockError" in text for text in captured))
		self.assertEqual(frappe.db.count(OUTBOX, {"reference_name": self.release}), 0)
		self.assert_unposted()

	def test_acknowledgement_proof_session_and_revision_are_all_required(self):
		preview = self.success(self.preview())
		self.assertEqual(self.confirm(preview, acknowledge=False).status_code, 400)
		self.assertEqual(self.confirm(preview, proof="a" * 43).status_code, 403)
		self.assertEqual(self.preview(revision=2).status_code, 409)
		self.login_actor(source_fixture.MANAGER)
		self.assertEqual(self.confirm(preview).status_code, 403)
		fresh = self.success(self.preview())
		self.success(self.confirm(fresh))
		self.assert_unposted()

	def test_changed_prepared_intent_invalidates_a_previous_confirmation(self):
		preview = self.success(self.preview())
		self.success(
			self.post_command(
				"release.revise",
				dict(
					release=self.release,
					expected_revision=1,
					posting_date="2026-10-27",
					reason="重新核实预算下达日期",
				),
			)
		)
		self.assertEqual(self.confirm(preview).status_code, 409)
		self.assertEqual(frappe.db.count(OUTBOX, {"reference_name": self.release}), 0)
		self.assert_unposted()

	def test_current_source_share_revocation_denies_confirmation(self):
		preview = self.success(self.preview())
		frappe.set_user("Administrator")
		frappe.share.remove("HRP Hospital", source_fixture.HOSPITAL, source_fixture.MANAGER)
		frappe.local.db.commit()
		try:
			self.assertEqual(self.confirm(preview).status_code, 403)
			self.assertEqual(frappe.db.count(OUTBOX, {"reference_name": self.release}), 0)
		finally:
			frappe.set_user("Administrator")
			share_test_records("HRP Hospital", (source_fixture.HOSPITAL,), (source_fixture.MANAGER,))
			frappe.local.db.commit()
		self.assert_unposted()

	def test_current_financial_field_revocation_denies_confirmation(self):
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter

		preview = self.success(self.preview())
		frappe.set_user("Administrator")
		setter = make_property_setter(
			"HRP Budget Plan", "total_amount", "permlevel", 2, "Int", validate_fields_for_doctype=False
		)
		frappe.local.db.commit()
		try:
			self.assertEqual(self.confirm(preview).status_code, 403)
			self.assertEqual(frappe.db.count(OUTBOX, {"reference_name": self.release}), 0)
		finally:
			frappe.set_user("Administrator")
			frappe.delete_doc("Property Setter", setter.name, ignore_permissions=True)
			frappe.clear_cache(doctype="HRP Budget Plan")
			frappe.local.db.commit()
		self.assert_unposted()

	def test_api_credentials_cannot_preview_or_authorize_financial_actions(self):
		from frappe.core.doctype.user.user import generate_keys

		preview = self.success(self.preview())
		frappe.set_user("Administrator")
		secret = generate_keys(source_fixture.MANAGER)["api_secret"]
		api_key = frappe.db.get_value("User", source_fixture.MANAGER, "api_key")
		frappe.local.db.commit()
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		for endpoint, payload in (
			("prepare", dict(release=self.release, expected_revision=1)),
			("confirm", dict(confirmation=preview["confirmation"], proof=preview["proof"], acknowledge=True)),
		):
			response = self.post(
				self.method(source_fixture.PREFIX + "release." + endpoint),
				{"payload": payload},
				headers={
					"Authorization": f"token {api_key}:{secret}",
					"Idempotency-Key": fixture_request("token-denied"),
				},
			)
			self.assertEqual(response.status_code, 403)
		self.assertEqual(frappe.db.count(OUTBOX, {"reference_name": self.release}), 0)
		self.assert_unposted()

	def test_missing_release_rule_cannot_mint_a_financial_confirmation(self):
		frappe.set_user("Administrator")
		rule = frappe.get_doc("HRP Business Rule", "COD033-RELEASE-RELEASE")
		policy, _ = rule.runtime_definition()
		UpsertBusinessRule().execute(
			dict(policy=rule_payload(dict(policy, enabled=False)), expected_revision=int(rule.revision)),
			idempotency_key=fixture_request("release-rule-off"),
		)
		frappe.local.db.commit()
		try:
			self.assertEqual(self.preview().status_code, 409)
			self.assertEqual(frappe.db.count(RELEASE_ENTRY, {"release": self.release}), 0)
		finally:
			frappe.set_user("Administrator")
			UpsertBusinessRule().execute(
				dict(policy=policy, expected_revision=int(rule.revision) + 1),
				idempotency_key=fixture_request("release-rule-on"),
			)
			frappe.local.db.commit()
		self.assert_unposted()
