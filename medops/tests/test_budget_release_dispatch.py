from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from decimal import Decimal, localcontext
from unittest.mock import patch

from rq import Worker
from rq.job import Job

import frappe
from frappe.tests.test_api import FrappeAPITestCase
from frappe.utils import get_bench_path
from frappe.utils.background_jobs import create_job_id, get_queue, get_redis_conn

from medops.common.budget_ledger import LEDGER, RELEASE, RELEASE_ENTRY
from medops.common.budget_release_control import claim_receipt_key
from medops.common.domain_service import fingerprint_json
from medops.common.error_catalog import IoneApplicationError
from medops.common.integration_outbox import OUTBOX
from medops.hrp_budget.jobs import dispatch
from medops.hrp_budget.jobs import release as jobs
from medops.hrp_budget.services import release_confirmation as confirmation
from medops.services.audit_context import ensure_audit_context
from medops.services.errors import raise_ione_error
from medops.tests import test_budget_consolidation as source_fixture
from medops.tests import test_budget_release_execution as execution_fixture
from medops.tests.test_budget_release_preparation import release_rule_fixtures
from medops.tests.test_budget_submission import fixture_request

SCHEDULER = "medops.hrp_budget.jobs.release.dispatch_due_releases"


class TestBudgetReleaseDispatch(FrappeAPITestCase):
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

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.org, cls.units, cls.value, cls.fund = source_fixture.fixtures()
		release_rule_fixtures()

	def setUp(self):
		super().setUp()
		self.new_release()
		primary = frappe.local.db
		try:
			with self.secondary_connection():
				self._secondary_connection.rollback()
		finally:
			frappe.local.db = primary

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		super().tearDown()

	def test_confirmation_contacts_queue_only_after_independent_connection_sees_authority(self):
		preview = self.success(self.preview())
		observed = []

		def enqueue(method, **kwargs):
			primary = frappe.local.db
			with self.secondary_connection():
				self._secondary_connection.rollback()
				parent = frappe.db.get_value(RELEASE, self.release, ["status", "docstatus", "revision"])
				status = frappe.db.get_value(OUTBOX, kwargs["message"], "status")
			# Assertions belong outside the production callback's error boundary;
			# a swallowed AssertionError must never make this test look successful.
			observed.append((method, kwargs, parent, status, frappe.local.db is primary))

		with patch("frappe.enqueue", side_effect=enqueue) as queue:
			self.success(self.confirm(preview, allow_dispatch=True))
		self.assertEqual(queue.call_count, 1)
		self.assertEqual(len(observed), 1)
		method, kwargs, parent, status, restored = observed[0]
		self.assertEqual((parent, status, restored), (("Authorized", 0, 2), "Pending", True))
		self.assertEqual(method, jobs.CLAIM_JOB)
		self.assertFalse(kwargs["enqueue_after_commit"])
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["queue"], "long")
		self.assertEqual(kwargs["timeout"], 300)
		self.assertEqual(
			set(kwargs), {"queue", "timeout", "enqueue_after_commit", "job_id", "deduplicate", "message"}
		)
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 0)

	def test_savepoint_rollback_discards_financial_rows_even_when_callback_remains_registered(self):
		preview = self.success(self.preview())
		original = confirmation.confirm_release_authorization

		def rolled_back(*args, **kwargs):
			frappe.db.savepoint("cod033_dispatch_outer")
			result = original(*args, **kwargs)
			frappe.db.rollback(save_point="cod033_dispatch_outer")
			return result

		with (
			patch.object(confirmation, "confirm_release_authorization", side_effect=rolled_back),
			patch("frappe.enqueue") as queue,
		):
			self.success(self.confirm(preview, allow_dispatch=True))
		queue.assert_not_called()
		self.assertEqual(frappe.db.get_value(RELEASE, self.release, ["status", "revision"]), ("Prepared", 1))
		self.assertEqual(frappe.db.count(OUTBOX, {"reference_name": self.release}), 0)
		self.assertEqual(
			frappe.db.count(RELEASE_ENTRY, {"release": self.release, "event_kind": "Decision"}), 0
		)

	def test_queue_failure_preserves_committed_authority_and_scheduler_can_retry(self):
		preview = self.success(self.preview())
		with (
			patch("frappe.enqueue", side_effect=ConnectionError("synthetic private queue failure detail")),
			patch.object(dispatch, "emit_audit_event") as audit,
		):
			self.success(self.confirm(preview, allow_dispatch=True))
		self.assertEqual(frappe.db.get_value(RELEASE, self.release, "status"), "Authorized")
		message = frappe.db.get_value(RELEASE, self.release, "outbox_message")
		self.assertEqual(frappe.db.get_value(OUTBOX, message, "status"), "Pending")
		audit.assert_called_once_with(
			"budget_release_dispatch_deferred",
			logger_name="medops.budget",
			kind="authorize",
			cause_type="ConnectionError",
		)
		self.assertNotIn("private queue", str(audit.call_args))
		with (
			patch.object(jobs, "due_release_messages", return_value=[message]),
			patch("frappe.enqueue") as queue,
		):
			self.assertEqual(self.run_job(SCHEDULER), {"dispatched": 1})
		queue.assert_called_once()
		self.assertEqual(queue.call_args.args, (jobs.CLAIM_JOB,))
		self.assertEqual(queue.call_args.kwargs["message"], message)

	def test_real_redis_rollback_and_duplicate_callbacks_do_not_duplicate_the_owned_job(self):
		self.authorize()
		queue = get_queue("long")
		before = queue.count
		connection = get_redis_conn()
		self.assertFalse(Worker.all(connection=connection))
		identifier = create_job_id("medops-release-claim-" + self.message)
		try:
			with patch("frappe.enqueue") as enqueue:
				dispatch.dispatch_claim(self.message)
				enqueue.assert_not_called()
			frappe.db.rollback()
			frappe.local.db.commit()
			self.assertEqual(queue.count, before)
			dispatch.dispatch_claim(self.message)
			dispatch.dispatch_claim(self.message)
			self.assertEqual(queue.count, before)
			frappe.local.db.commit()
			self.assertEqual(queue.count, before + 1)
			job = Job.fetch(identifier, connection=connection)
			self.assertEqual(job.get_status(), "queued")
			self.assertEqual(job.kwargs["method"], jobs.CLAIM_JOB)
			self.assertEqual(job.kwargs["kwargs"], {"message": self.message})
		finally:
			if queue.count != before:
				job = Job.fetch(identifier, connection=connection)
				self.assertEqual(job.get_status(), "queued")
				self.assertEqual(job.kwargs["kwargs"], {"message": self.message})
				job.delete()
			self.assertEqual(queue.count, before)

	def test_rejected_confirmation_result_registers_no_queue_callback(self):
		preview = self.success(self.preview())

		def denied(_result):
			raise_ione_error("PERMISSION_DENIED")

		with (
			patch.object(confirmation.ConfirmReleaseAuthorization, "authorize_result", side_effect=denied),
			patch("frappe.enqueue") as queue,
		):
			self.assertEqual(self.confirm(preview, allow_dispatch=True).status_code, 403)
		queue.assert_not_called()
		self.assertEqual(frappe.db.count(OUTBOX, {"reference_name": self.release}), 0)
		self.assertEqual(frappe.db.get_value(RELEASE, self.release, "status"), "Prepared")

	def test_superseded_authorization_hint_is_skipped_and_only_current_epoch_is_enqueued(self):
		self.authorize()
		old = self.message
		self.success(self.confirm(self.success(self.preview(revision=2))))
		current = frappe.db.get_value(RELEASE, self.release, "outbox_message")
		self.assertNotEqual(old, current)
		correlation_id = ensure_audit_context().correlation_id
		with patch("frappe.enqueue") as queue:
			dispatch.dispatch_authorized_release(self.release, old, 2, correlation_id=correlation_id)
			dispatch.dispatch_authorized_release(self.release, current, 3, correlation_id=correlation_id)
			queue.assert_not_called()
			frappe.local.db.commit()
		queue.assert_called_once()
		self.assertEqual(queue.call_args.kwargs["message"], current)

	def test_cron_metadata_and_infrastructure_role_or_request_guards(self):
		self.assertIn(SCHEDULER, frappe.get_hooks("scheduler_events")["cron"]["* * * * *"])
		self.assertEqual(
			frappe.db.get_value("Scheduled Job Type", {"method": SCHEDULER}, ["frequency", "cron_format"]),
			("Cron", "* * * * *"),
		)
		with patch("frappe.enqueue") as queue:
			frappe.set_user(source_fixture.MANAGER)
			with self.assertRaises(IoneApplicationError):
				jobs.dispatch_due_releases()
			frappe.set_user("Administrator")
			previous = getattr(frappe.local, "request", None)
			frappe.local.request = object()
			try:
				with self.assertRaises(IoneApplicationError):
					jobs.dispatch_due_releases()
			finally:
				frappe.local.request = previous
		queue.assert_not_called()

	def test_real_http_confirmation_and_standard_rq_worker_post_exactly_one_complete_batch(self):
		queue = get_queue("long")
		connection = get_redis_conn()
		self.assertEqual(queue.count, 0, "Refuse to consume pre-existing long-queue work")
		self.assertFalse(Worker.all(connection=connection))
		preview = self.success(self.preview())
		key = fixture_request("automatic-rq-confirm")
		self.success(self.confirm(preview, key=key, allow_dispatch=True))
		message = frappe.db.get_value(RELEASE, self.release, "outbox_message")
		self.assertEqual(queue.count, 1)
		claim = queue.jobs[0]
		self.assertEqual(claim.kwargs["method"], jobs.CLAIM_JOB)
		self.assertEqual(claim.kwargs["kwargs"], {"message": message})
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 0)

		def unchanged_proof():
			tables = (
				"HRP Budget Version",
				"HRP Budget Plan",
				"HRP Budget Plan Line",
				"HRP Budget Submission",
				"HRP Budget Submission Line",
				"HRP Budget Approval",
				"HRP Budget Review",
			)
			rows = {table: frappe.get_all(table, fields=["*"], order_by="name asc") for table in tables}
			rows["roles"] = frappe.get_all(
				"Has Role",
				filters={"parenttype": ["in", ["User", "Role Profile"]]},
				fields=["*"],
				order_by="name asc",
			)
			digest = hashlib.sha256(
				json.dumps(json.loads(frappe.as_json(rows)), sort_keys=True, ensure_ascii=False).encode()
			).hexdigest()
			return digest, {
				name: frappe.db.count(name) for name in ("GL Entry", "Stock Ledger Entry", "Error Log")
			}

		before = unchanged_proof()
		other_queues = {name: get_queue(name).count for name in ("short", "default")}
		bench = shutil.which("bench")
		assert bench is not None, "The required native bench CLI is unavailable"
		self.assertEqual(frappe.db.transaction_writes, 0)
		execution_id = create_job_id(
			"medops-release-execute-" + fingerprint_json([message, claim_receipt_key(message, 1)])
		)
		try:
			with tempfile.TemporaryFile() as log:
				worker = subprocess.Popen(
					[bench, "worker", "--queue", "long", "--burst"],
					cwd=get_bench_path(),
					stdout=log,
					stderr=log,
					stdin=subprocess.DEVNULL,
					start_new_session=True,
				)
				try:
					self.assertEqual(worker.wait(timeout=180), 0)
				except subprocess.TimeoutExpired:
					# This process group was created solely for this synthetic test.
					if sys.platform == "win32":
						worker.kill()
					else:
						self.assertEqual(os.getpgid(worker.pid), worker.pid)
						os.killpg(worker.pid, signal.SIGKILL)
					worker.wait(timeout=15)
					raise
			frappe.db.rollback()
			self.assertEqual(
				frappe.db.get_value(
					RELEASE, self.release, ["status", "docstatus", "revision", "ledger_count"]
				),
				("Posted", 1, 3, 2),
			)
			self.assertEqual(
				frappe.db.get_value(OUTBOX, message, ["status", "attempt_count"]), ("Delivered", 1)
			)
			self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 2)
			self.assertEqual(
				frappe.db.get_value(RELEASE, self.release, ["total_amount", "currency_precision"]),
				("100000.000000001", 9),
			)
			rows = frappe.get_all(
				LEDGER,
				filters={"voucher_no": self.release},
				fields=["amount", "debit", "credit", "is_reversal"],
			)
			self.assertTrue(
				all(
					row.amount == row.debit and Decimal(row.credit) == 0 and not row.is_reversal
					for row in rows
				)
			)
			with localcontext() as context:
				context.prec = 48
				self.assertEqual(sum(Decimal(row.amount) for row in rows), Decimal("100000.000000001"))
			self.assertEqual(Job.fetch(claim.id, connection=connection).get_status(), "finished")
			self.assertEqual(Job.fetch(execution_id, connection=connection).get_status(), "finished")
			self.assertEqual(unchanged_proof(), before)
			self.assertEqual({name: get_queue(name).count for name in other_queues}, other_queues)
			self.assertEqual(queue.count, 0)
			self.assertFalse(Worker.all(connection=connection))
			self.assertTrue(
				self.success(self.confirm(preview, key=key, allow_dispatch=True))["idempotency_replayed"]
			)
			self.assertEqual(queue.count, 0)
		finally:
			for identifier in (claim.id, execution_id):
				if identifier in queue.job_ids:
					job = Job.fetch(identifier, connection=connection)
					self.assertEqual(job.get_status(), "queued")
					self.assertEqual(job.kwargs["kwargs"]["message"], message)
					job.delete()
