from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, localcontext
from unittest.mock import patch

import frappe
from frappe.tests.test_api import FrappeAPITestCase
from frappe.utils import get_datetime, now_datetime
from frappe.utils.background_jobs import execute_job

from medops.common.budget_ledger import LEDGER, RELEASE
from medops.common.budget_release_control import ReleaseExecutionLease, release_execution
from medops.common.error_catalog import IoneApplicationError
from medops.common.integration_outbox import OUTBOX
from medops.hrp_budget.jobs import release as jobs
from medops.hrp_budget.services import control_release
from medops.hrp_budget.services import release_execution as execution
from medops.hrp_budget.services.control_guards import assert_budget_guards
from medops.hrp_budget.services.release_access import get_release
from medops.hrp_budget.services.release_sources import stored_release_facts
from medops.services.errors import raise_ione_error
from medops.services.immutable_ledger import append_immutable_entry
from medops.tests import test_budget_consolidation as source_fixture
from medops.tests import test_budget_release_confirmation as confirmation_fixture
from medops.tests.scope_fixtures import share_test_records
from medops.tests.test_budget_release_preparation import release_rule_fixtures


class TestBudgetReleaseExecution(FrappeAPITestCase):
	login_actor = confirmation_fixture.TestBudgetReleaseConfirmation.login_actor
	success = confirmation_fixture.TestBudgetReleaseConfirmation.success
	post_command = confirmation_fixture.TestBudgetReleaseConfirmation.post_command
	request_approval = confirmation_fixture.TestBudgetReleaseConfirmation.request_approval
	decide = confirmation_fixture.TestBudgetReleaseConfirmation.decide
	new_sources = confirmation_fixture.TestBudgetReleaseConfirmation.new_sources
	proposal = confirmation_fixture.TestBudgetReleaseConfirmation.proposal
	seal = confirmation_fixture.TestBudgetReleaseConfirmation.seal
	new_release = confirmation_fixture.TestBudgetReleaseConfirmation.new_release
	preview = confirmation_fixture.TestBudgetReleaseConfirmation.preview
	confirm = confirmation_fixture.TestBudgetReleaseConfirmation.confirm

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.org, cls.units, cls.value, cls.fund = source_fixture.fixtures()
		release_rule_fixtures()

	def setUp(self):
		super().setUp()
		self.new_release()
		self.authorize()
		# The pinned lazy secondary_connection creates its connection before
		# capturing current_conn. Initialize while explicitly retaining the real
		# HTTP/job primary, so later revocation callbacks restore that connection.
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

	def authorize(self):
		self.success(self.confirm(self.success(self.preview())))
		self.message = frappe.db.get_value(RELEASE, self.release, "outbox_message")

	def run_job(self, method, **kwargs):
		frappe.db.rollback()
		previous_request = getattr(frappe.local, "request", None)
		previous_job = getattr(frappe.local, "job", None)
		frappe.local.request = None
		try:
			return execute_job(
				site=frappe.local.site,
				method=method,
				event=None,
				job_name="COD033 private execution",
				kwargs=kwargs,
				user="Administrator",
				is_async=False,
			)
		finally:
			frappe.local.request = previous_request
			frappe.local.job = previous_job
			frappe.set_user("Administrator")

	def claim(self, attempt=1):
		with patch("frappe.enqueue") as enqueue:
			result = self.run_job(jobs.CLAIM_JOB, message=self.message)
		self.assertEqual(result["status"], "Dispatched")
		args, kwargs = enqueue.call_args
		self.assertEqual(args, (jobs.EXECUTE_JOB,))
		# The owned callback runs after the actual framework job commit.
		self.assertFalse(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["timeout"], execution.EXECUTION_TIMEOUT)
		self.assertEqual(
			set(kwargs) - {"queue", "timeout", "enqueue_after_commit", "job_id", "deduplicate"},
			{"message", "receipt"},
		)
		self.assertEqual(kwargs["receipt"], jobs.claim_receipt_key(self.message, attempt))
		return kwargs["receipt"]

	def execute(self, receipt):
		return self.run_job(jobs.EXECUTE_JOB, message=self.message, receipt=receipt)

	def assert_failed_atomic(self, result):
		self.assertEqual(result["status"], "Failed")
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 0)
		self.assertEqual(
			frappe.db.get_value(RELEASE, self.release, ["status", "docstatus", "revision"]),
			("Authorized", 0, 2),
		)
		self.assertEqual(frappe.db.get_value(OUTBOX, self.message, "status"), "Failed")

	def test_actual_framework_jobs_post_and_completely_reverse_exact_original_grain(self):
		before = {doctype: frappe.db.count(doctype) for doctype in ("GL Entry", "Stock Ledger Entry")}
		receipt = self.claim()
		self.assertEqual(self.execute(receipt)["status"], "Posted")
		first = get_release(self.release)
		stored_release_facts(first)
		self.assertEqual(
			(first.status, first.docstatus, first.revision, first.ledger_count), ("Posted", 1, 3, 2)
		)
		self.assertEqual(frappe.db.get_value(OUTBOX, self.message, "status"), "Delivered")
		original_rows = frappe.get_all(LEDGER, filters={"voucher_no": self.release}, fields=["*"])
		self.assertEqual(len(original_rows), 2)
		with localcontext() as context:
			context.prec = 48
			self.assertEqual(sum(Decimal(row.amount) for row in original_rows), Decimal("100000.000000001"))
		self.assertEqual({row.organization_unit for row in original_rows}, {self.units["A"], self.units["B"]})
		self.assertEqual(self.execute(receipt)["status"], "Posted")
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 2)
		original_release = self.release
		self.login_actor(source_fixture.MANAGER)
		self.release = self.success(
			self.post_command(
				"release.create_reversal",
				dict(
					release=self.release,
					expected_revision=3,
					posting_date="2026-10-27",
					reason="合成完整反向验收",
				),
			)
		)["name"]
		self.authorize()
		self.assertEqual(self.execute(self.claim())["status"], "Posted")
		back = frappe.get_all(LEDGER, filters={"voucher_no": self.release}, fields=["*"])
		self.assertEqual(len(back), 2)
		by_name = {row.name: row for row in original_rows}
		for row in back:
			original = by_name[row.reversal_of]
			self.assertEqual(Decimal(row.amount), -Decimal(original.amount))
			self.assertEqual((row.debit, row.credit), (original.credit, original.debit))
			for field in (
				"organization_unit",
				"cost_center",
				"currency_precision",
				"origin_plan",
				"origin_version",
				"fact_key",
				"source_submission",
				"source_approval",
			):
				self.assertEqual(row[field], original[field])
			self.assertEqual(row.is_reversal, 1)
		self.assertEqual(
			original_rows, frappe.get_all(LEDGER, filters={"voucher_no": original_release}, fields=["*"])
		)
		stored_release_facts(get_release(self.release))
		self.assertEqual(before, {doctype: frappe.db.count(doctype) for doctype in before})

	def test_second_row_failure_rolls_back_batch_parent_and_delivery_then_retries(self):
		receipt = self.claim()
		calls = 0

		def fail_second(*args, **kwargs):
			nonlocal calls
			calls += 1
			if calls == 2:
				raise RuntimeError("synthetic failure after first native append")
			return append_immutable_entry(*args, **kwargs)

		with patch.object(execution, "append_immutable_entry", side_effect=fail_second):
			self.assert_failed_atomic(self.execute(receipt))
		future = now_datetime() + timedelta(seconds=61)
		with (
			patch.object(jobs, "now_datetime", return_value=future),
			patch("medops.services.transactional_message.now_datetime", return_value=future),
		):
			receipt = self.claim(attempt=2)
		self.assertEqual(self.execute(receipt)["status"], "Posted")
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 2)

	def test_superseded_authorization_cannot_execute_an_old_claim(self):
		old_message = self.message
		old_receipt = self.claim()
		self.login_actor(source_fixture.MANAGER)
		self.success(self.confirm(self.success(self.preview(revision=2))))
		new_message = frappe.db.get_value(RELEASE, self.release, "outbox_message")
		self.assertNotEqual(old_message, new_message)
		self.assertEqual(self.execute(old_receipt)["status"], "Failed")
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 0)
		self.message = new_message
		self.assertEqual(self.execute(self.claim())["status"], "Posted")
		self.assertEqual(frappe.db.get_value(RELEASE, self.release, "revision"), 4)

	def test_unowned_backend_and_foreign_claim_receipt_cannot_post(self):
		receipt = self.claim()
		frappe.set_user(source_fixture.MANAGER)
		with self.assertRaises(IoneApplicationError):
			jobs.load_claim_receipt(self.message, receipt)
		with self.assertRaises(IoneApplicationError):
			execution.ExecuteBudgetRelease().execute(
				execution.ExecuteRelease(self.message), idempotency_key="unowned-budget-execution"
			)
		frappe.set_user("Administrator")
		claim = jobs.load_claim_receipt(self.message, receipt)
		message = frappe.get_doc(OUTBOX, self.message)
		authority, _ = execution.message_authority(message)
		lease = ReleaseExecutionLease(
			authority,
			self.message,
			1,
			get_datetime(claim["lease_expires_at"]),
			str(claim["processing_token"]),
		)
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.begin()
		frappe.set_user(source_fixture.MANAGER)
		with release_execution(lease), self.assertRaises(IoneApplicationError):
			execution.ExecuteBudgetRelease().execute(
				execution.ExecuteRelease(self.message), idempotency_key="old-snapshot-budget-execution"
			)
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 0)

	@contextmanager
	def revoke_after_first_row(self, revoke):
		calls = 0

		def append_then_revoke(*args, **kwargs):
			nonlocal calls
			result = append_immutable_entry(*args, **kwargs)
			calls += 1
			if calls == 1:
				primary = frappe.local.db
				with self.secondary_connection():
					frappe.set_user("Administrator")
					try:
						revoke()
						self._secondary_connection.commit()
					except Exception:
						self._secondary_connection.rollback()
						raise
				self.assertIs(frappe.local.db, primary)
				frappe.set_user(source_fixture.MANAGER)
			return result

		with patch.object(execution, "append_immutable_entry", side_effect=append_then_revoke):
			yield lambda: calls
		self.assertGreaterEqual(calls, 1)

	def test_current_share_revocation_after_first_write_rolls_back_the_whole_transaction(self):
		receipt = self.claim()

		def revoke():
			frappe.share.remove("HRP Hospital", source_fixture.HOSPITAL, source_fixture.MANAGER)

		try:
			with self.revoke_after_first_row(revoke):
				self.assert_failed_atomic(self.execute(receipt))
		finally:
			frappe.set_user("Administrator")
			share_test_records("HRP Hospital", (source_fixture.HOSPITAL,), (source_fixture.MANAGER,))
			frappe.local.db.commit()

	def test_current_role_revocation_wins_over_poisoned_redis_roles_mid_transaction(self):
		receipt = self.claim()
		roles = frappe.get_roles(source_fixture.MANAGER)

		def revoke():
			frappe.get_doc("User", source_fixture.MANAGER).remove_roles("HRP Budget Manager")
			frappe.db.after_commit.add(lambda: frappe.cache.hset("roles", source_fixture.MANAGER, roles))

		try:
			with self.revoke_after_first_row(revoke) as appended:
				self.assert_failed_atomic(self.execute(receipt))
				self.assertEqual(appended(), 2)
		finally:
			frappe.set_user("Administrator")
			frappe.get_doc("User", source_fixture.MANAGER).add_roles("HRP Budget Manager")
			frappe.local.db.commit()
			frappe.cache.hdel("roles", source_fixture.MANAGER)

	def test_current_field_permission_revocation_mid_transaction_rolls_back_all_rows(self):
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter

		receipt = self.claim()
		setters = []

		def revoke():
			setters.append(
				make_property_setter(
					"HRP Budget Plan",
					"total_amount",
					"permlevel",
					2,
					"Int",
					validate_fields_for_doctype=False,
				).name
			)

		try:
			with self.revoke_after_first_row(revoke):
				self.assert_failed_atomic(self.execute(receipt))
		finally:
			frappe.set_user("Administrator")
			for name in setters:
				frappe.delete_doc("Property Setter", name, ignore_permissions=True)
			frappe.clear_cache(doctype="HRP Budget Plan")
			frappe.local.db.commit()

	def test_current_data_scope_revocation_after_two_writes_rolls_back_the_complete_batch(self):
		receipt = self.claim()
		scope_name = "COD032C-scope-" + source_fixture.MANAGER.split("@")[0]

		def revoke():
			scope = frappe.get_doc("HRP Access Scope", scope_name)
			self.assertEqual(scope.company, source_fixture.COMPANY)
			self.assertEqual(scope.hospital, source_fixture.HOSPITAL)
			scope.enabled = 0
			scope.save(ignore_permissions=True)

		try:
			with self.revoke_after_first_row(revoke) as appended:
				self.assert_failed_atomic(self.execute(receipt))
				self.assertEqual(appended(), 2)
		finally:
			frappe.set_user("Administrator")
			scope = frappe.get_doc("HRP Access Scope", scope_name)
			scope.enabled = 1
			scope.save(ignore_permissions=True)
			frappe.local.db.commit()

	def test_current_native_user_permission_wins_over_poisoned_redis_permissions_after_two_writes(self):
		from frappe.core.doctype.user_permission.user_permission import get_user_permissions

		frappe.set_user("Administrator")
		centers = tuple(
			frappe.db.get_value(
				"HRP Organization Mapping", {"organization_unit": self.units[code]}, "cost_center"
			)
			for code in ("A", "B")
		)
		permissions = []
		had_role = "Accounts User" in frappe.get_roles(source_fixture.MANAGER)
		try:
			frappe.get_doc("User", source_fixture.MANAGER).add_roles("Accounts User")
			for center in centers:
				permissions.append(
					frappe.get_doc(
						dict(
							doctype="User Permission",
							user=source_fixture.MANAGER,
							allow="Cost Center",
							for_value=center,
							apply_to_all_doctypes=1,
							hide_descendants=1,
						)
					)
					.insert(ignore_permissions=True)
					.name
				)
				frappe.share.remove("Cost Center", center, source_fixture.MANAGER)
			frappe.local.db.commit()
			frappe.set_user(source_fixture.MANAGER)
			get_release(self.release)
			self.assertTrue(
				frappe.has_permission("Cost Center", "read", doc=frappe.get_doc("Cost Center", centers[1]))
			)
			old_permissions = deepcopy(get_user_permissions(source_fixture.MANAGER))
			receipt = self.claim()

			def revoke():
				frappe.delete_doc("User Permission", permissions[1], ignore_permissions=True)
				frappe.cache.hdel("user_permissions", source_fixture.MANAGER)
				self.assertFalse(
					frappe.has_permission(
						"Cost Center",
						"read",
						doc=frappe.get_doc("Cost Center", centers[1]),
						user=source_fixture.MANAGER,
					)
				)
				frappe.db.after_commit.add(
					lambda: frappe.cache.hset("user_permissions", source_fixture.MANAGER, old_permissions)
				)

			with self.revoke_after_first_row(revoke) as appended:
				self.assert_failed_atomic(self.execute(receipt))
				self.assertEqual(appended(), 2)
		finally:
			frappe.set_user("Administrator")
			for name in permissions:
				if frappe.db.exists("User Permission", name):
					frappe.delete_doc("User Permission", name, ignore_permissions=True)
			share_test_records("Cost Center", centers, (source_fixture.MANAGER,))
			if not had_role:
				frappe.get_doc("User", source_fixture.MANAGER).remove_roles("Accounts User")
			frappe.local.db.commit()
			frappe.cache.hdel("user_permissions", source_fixture.MANAGER)

	def test_persisted_authorized_release_guard_checks_original_units_without_posting(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		revision = int(frappe.db.get_value(RELEASE, self.release, "revision"))
		with self.assertRaises(IoneApplicationError):
			control_release.prepare_persisted_release_guards(self.release, revision + 1)
		self.assertIsNone(getattr(frappe.local.db, "_medops_budget_guard_marker", None))
		keys = control_release.prepare_persisted_release_guards(self.release, revision)
		self.assertEqual({key.organization_unit for key in keys}, {self.units["A"], self.units["B"]})
		assert_budget_guards(keys)
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 0)
		frappe.db.rollback()

	def test_persisted_release_guard_poisoned_when_actor_check_is_revoked(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		revision = int(frappe.db.get_value(RELEASE, self.release, "revision"))
		original = control_release.require_financial_actor
		calls = []

		def revoke_after_guard(doc):
			calls.append(1)
			if len(calls) == 2:
				raise_ione_error("PERMISSION_DENIED")
			return original(doc)

		with patch.object(control_release, "require_financial_actor", side_effect=revoke_after_guard):
			with self.assertRaises(IoneApplicationError):
				control_release.prepare_persisted_release_guards(self.release, revision)
		self.assertEqual(len(calls), 2)
		marker = getattr(frappe.local.db, "_medops_budget_guard_marker", None)
		assert marker is not None
		self.assertTrue(marker.state.invalidated)
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards(marker.keys)
		frappe.db.rollback()
		self.assertIsNone(getattr(frappe.local.db, "_medops_budget_guard_marker", None))
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 0)

	def test_persisted_reversal_guard_verifies_the_complete_original_batch(self):
		receipt = self.claim()
		self.assertEqual(self.execute(receipt)["status"], "Posted")
		original_release = self.release
		self.login_actor(source_fixture.MANAGER)
		self.release = self.success(
			self.post_command(
				"release.create_reversal",
				dict(
					release=original_release,
					expected_revision=3,
					posting_date="2026-10-27",
					reason="合成完整反向验收",
				),
			)
		)["name"]
		self.authorize()
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		revision = int(frappe.db.get_value(RELEASE, self.release, "revision"))
		with patch.object(
			control_release, "complete_batch", wraps=control_release.complete_batch
		) as complete:
			keys = control_release.prepare_persisted_release_guards(self.release, revision)
		self.assertEqual(complete.call_count, 2)
		self.assertEqual({key.organization_unit for key in keys}, {self.units["A"], self.units["B"]})
		assert_budget_guards(keys)
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": original_release}), 2)
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 0)
		frappe.db.rollback()
