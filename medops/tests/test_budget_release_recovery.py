from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.test_api import FrappeAPITestCase
from frappe.utils import get_datetime, now_datetime

from medops.common.budget_ledger import LEDGER, RELEASE
from medops.common.error_catalog import IoneApplicationError
from medops.common.integration_outbox import OUTBOX, OutboxRetirement, outbox_retirement
from medops.hrp_budget.jobs import release as jobs
from medops.hrp_budget.services import release_execution as execution
from medops.hrp_budget.services.release_recovery import EXHAUSTED, SUPERSEDED
from medops.hrp_integration.services.outbox import RetireIntegrationOutbox
from medops.services.transactional_message import RetireOutboxMessage
from medops.tests import test_budget_consolidation as source_fixture
from medops.tests import test_budget_release_execution as execution_fixture
from medops.tests.test_budget_release_preparation import release_rule_fixtures
from medops.tests.test_budget_submission import fixture_request


@contextmanager
def recovery_clock(value):
	with ExitStack() as stack:
		for name in (
			"medops.hrp_budget.jobs.release.now_datetime",
			"medops.hrp_budget.services.release_recovery.now_datetime",
			"medops.hrp_integration.services.outbox.now_datetime",
			"medops.services.transactional_message.now_datetime",
		):
			stack.enter_context(patch(name, return_value=value))
		yield


class TestBudgetReleaseRecovery(FrappeAPITestCase):
	login_actor = execution_fixture.TestBudgetReleaseExecution.login_actor
	success = execution_fixture.TestBudgetReleaseExecution.success
	post_command = execution_fixture.TestBudgetReleaseExecution.post_command
	request_approval = execution_fixture.TestBudgetReleaseExecution.request_approval
	decide = execution_fixture.TestBudgetReleaseExecution.decide
	new_sources = execution_fixture.TestBudgetReleaseExecution.new_sources
	proposal = execution_fixture.TestBudgetReleaseExecution.proposal
	seal = execution_fixture.TestBudgetReleaseExecution.seal
	new_release = execution_fixture.TestBudgetReleaseExecution.new_release
	preview = execution_fixture.TestBudgetReleaseExecution.preview
	confirm = execution_fixture.TestBudgetReleaseExecution.confirm
	authorize = execution_fixture.TestBudgetReleaseExecution.authorize
	run_job = execution_fixture.TestBudgetReleaseExecution.run_job
	claim = execution_fixture.TestBudgetReleaseExecution.claim
	execute = execution_fixture.TestBudgetReleaseExecution.execute
	assert_failed_atomic = execution_fixture.TestBudgetReleaseExecution.assert_failed_atomic

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.org, cls.units, cls.value, cls.fund = source_fixture.fixtures()
		release_rule_fixtures()

	def setUp(self):
		super().setUp()
		self.new_release()
		self.authorize()
		primary = frappe.local.db
		try:
			with self.secondary_connection():
				self._secondary_connection.rollback()
		finally:
			frappe.local.db = primary

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.begin()
		super().tearDown()

	def supersede(self):
		self.login_actor(source_fixture.MANAGER)
		self.success(self.confirm(self.success(self.preview(revision=2))))
		return frappe.db.get_value(RELEASE, self.release, "outbox_message")

	def assert_retired(self, reason, attempt):
		message = frappe.get_doc(OUTBOX, self.message)
		self.assertEqual(
			(message.status, message.last_error_code, message.attempt_count), ("Dead Letter", reason, attempt)
		)
		self.assertFalse(message.processing_token_hash)
		self.assertFalse(message.lease_expires_at)
		self.assertTrue(message.completed_at)
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 0)

	def test_superseded_pending_message_retires_without_inventing_attempts(self):
		new_message = self.supersede()
		with patch("frappe.enqueue") as enqueue:
			result = self.run_job(jobs.CLAIM_JOB, message=self.message)
		self.assertEqual(result["status"], "Dead Letter")
		enqueue.assert_not_called()
		self.assert_retired(SUPERSEDED, 0)
		self.assertEqual(self.run_job(jobs.CLAIM_JOB, message=self.message)["status"], "Skipped")
		self.message = new_message
		self.assertEqual(self.execute(self.claim())["status"], "Posted")

	def test_superseded_active_message_retires_and_old_receipt_cannot_post(self):
		receipt = self.claim()
		self.supersede()
		with patch("frappe.enqueue") as enqueue:
			self.assertEqual(self.run_job(jobs.CLAIM_JOB, message=self.message)["status"], "Dead Letter")
		enqueue.assert_not_called()
		self.assert_retired(SUPERSEDED, 1)
		self.assertEqual(self.execute(receipt)["status"], "Failed")
		self.assert_retired(SUPERSEDED, 1)

	def test_last_crashed_attempt_closes_even_after_encrypted_receipts_expire(self):
		base = now_datetime()
		for attempt in range(1, 11):
			with recovery_clock(base + timedelta(seconds=(attempt - 1) * (execution.LEASE_SECONDS + 1))):
				self.claim(attempt=attempt)
		with (
			recovery_clock(base + timedelta(days=2)),
			patch.object(
				jobs,
				"load_claim_receipt",
				side_effect=AssertionError("Recovery must not unseal or renew an expired receipt"),
			),
			patch("frappe.enqueue") as enqueue,
		):
			result = self.run_job(jobs.CLAIM_JOB, message=self.message)
		self.assertEqual(result["status"], "Dead Letter")
		enqueue.assert_not_called()
		self.assert_retired(EXHAUSTED, 10)

	def test_expired_earlier_claim_is_recovered_and_its_old_secret_cannot_change_new_attempt(self):
		old = self.claim()
		deadline = get_datetime(frappe.db.get_value(OUTBOX, self.message, "lease_expires_at"))
		with recovery_clock(deadline + timedelta(seconds=1)):
			current = self.claim(attempt=2)
		self.assertEqual(self.execute(old)["status"], "Failed")
		self.assertEqual(
			frappe.db.get_value(OUTBOX, self.message, ["status", "attempt_count"]), ("Processing", 2)
		)
		self.assertEqual(self.execute(current)["status"], "Posted")

	def test_retirement_requires_owned_capability_and_current_exhaustion_predicate(self):
		frappe.set_user("Administrator")
		message = frappe.get_doc(OUTBOX, self.message)
		command = RetireOutboxMessage(self.message, message.payload_hash, EXHAUSTED)
		with self.assertRaises(IoneApplicationError):
			RetireIntegrationOutbox().execute(command, idempotency_key=fixture_request("retire-unowned"))
		with (
			outbox_retirement(OutboxRetirement(self.message, message.payload_hash, 0, "Pending", EXHAUSTED)),
			self.assertRaises(IoneApplicationError),
		):
			RetireIntegrationOutbox().execute(command, idempotency_key=fixture_request("retire-before-lease"))
		self.claim()
		with (
			outbox_retirement(
				OutboxRetirement(self.message, message.payload_hash, 1, "Processing", EXHAUSTED)
			),
			self.assertRaises(IoneApplicationError),
		):
			RetireIntegrationOutbox().execute(command, idempotency_key=fixture_request("retire-active"))
		self.assertEqual(
			frappe.db.get_value(OUTBOX, self.message, ["status", "attempt_count"]), ("Processing", 1)
		)

	def test_posted_audit_sees_committed_state_on_an_independent_connection_and_is_not_reemitted(self):
		receipt = self.claim()
		observed = []

		def audit(event, **kwargs):
			if event != "budget_release_posted":
				return
			with self.secondary_connection():
				self._secondary_connection.rollback()
				observed.append(
					frappe.db.get_value(RELEASE, self.release, ["status", "docstatus", "revision"])
				)
			self.assertEqual(set(kwargs), {"logger_name", "line_count", "revision"})

		with patch.object(jobs, "emit_audit_event", side_effect=audit):
			self.assertEqual(self.execute(receipt)["status"], "Posted")
			self.assertEqual(self.execute(receipt)["status"], "Posted")
		self.assertEqual(observed, [("Posted", 1, 3)])

	def test_failed_final_validation_never_emits_a_posted_audit(self):
		receipt = self.claim()
		with (
			patch.object(
				execution.ExecuteBudgetRelease,
				"authorize_result",
				side_effect=RuntimeError("synthetic final denial"),
			),
			patch.object(jobs, "emit_audit_event") as audit,
			patch.object(execution, "emit_audit_event") as written,
		):
			self.assert_failed_atomic(self.execute(receipt))
		self.assertIn("budget_release_batch_written", [call.args[0] for call in written.call_args_list])
		self.assertNotIn("budget_release_posted", [call.args[0] for call in audit.call_args_list])
