from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal, localcontext
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests.test_api import FrappeAPITestCase
from frappe.utils import add_days, get_datetime, now_datetime, set_request
from frappe.utils.background_jobs import execute_job

from medops.common.budget_control import KEY_FIELDS, budget_control_key
from medops.common.budget_ledger import LEDGER, RELEASE
from medops.common.budget_material_match import match_purchase_release_keys
from medops.common.budget_release_control import ReleaseExecutionLease, release_execution
from medops.common.budget_reservation_anchor import reservation_anchor, reservation_anchor_row
from medops.common.budget_reservation_events import ReservationEvent
from medops.common.budget_reservation_proof import (
	ReservationSource,
	reservation_event_key,
	reservation_record,
)
from medops.common.budget_reservation_storage import reservation_record_row
from medops.common.error_catalog import IoneApplicationError
from medops.common.exact_money import exact_decimal_amount
from medops.common.integration_outbox import OUTBOX
from medops.common.material_source_snapshot import material_source_snapshot
from medops.common.reservation_authorization import reservation_intent
from medops.hrp_budget.jobs import release as jobs
from medops.hrp_budget.services import (
	control_inventory,
	control_release,
	initial_reservation_write,
	release_usage_guard,
	reservation_confirmation,
)
from medops.hrp_budget.services import release_execution as execution
from medops.hrp_budget.services.control_guards import acquire_budget_guards, assert_budget_guards
from medops.hrp_budget.services.material_budget_candidates import prepare_purchase_release_match
from medops.hrp_budget.services.material_request_source import locked_material_request_source
from medops.hrp_budget.services.material_reservation_anchor import verify_material_reservation_evidence
from medops.hrp_budget.services.material_reservation_observation import (
	guarded_material_reservation_observation,
)
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

	def test_release_job_holds_budget_guard_during_real_posting(self):
		original = execution.write_complete_batch
		observed = []

		def inspect_guard(doc, facts):
			primary = frappe.local.db
			marker = getattr(primary, "_medops_budget_guard_marker", None)
			self.assertIsNotNone(marker)
			assert marker is not None
			assert_budget_guards(marker.keys)
			with self.secondary_connection():
				other = frappe.local.db
				other.rollback()
				other.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
				other.sql("SET SESSION innodb_lock_wait_timeout = 1")
				other.begin()
				try:
					with self.assertRaises(frappe.QueryTimeoutError):
						acquire_budget_guards(marker.keys)
				finally:
					other.rollback()
			self.assertIs(frappe.local.db, primary)
			assert_budget_guards(marker.keys)
			observed.append(len(marker.keys))
			return original(doc, facts)

		with patch.object(execution, "write_complete_batch", side_effect=inspect_guard):
			self.assertEqual(self.execute(self.claim())["status"], "Posted")
		self.assertEqual(len(observed), 1)
		self.assertGreater(observed[0], 0)
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 2)

	def test_guard_deadlock_aborts_whole_job_and_new_attempt_posts_once(self):
		receipt = self.claim()
		calls = 0

		def conflict_after_first_row(*args, **kwargs):
			nonlocal calls
			calls += 1
			if calls == 2:
				raise frappe.QueryDeadlockError("synthetic lock conflict after first row")
			return append_immutable_entry(*args, **kwargs)

		with patch.object(execution, "append_immutable_entry", side_effect=conflict_after_first_row):
			self.assert_failed_atomic(self.execute(receipt))
		self.assertEqual(calls, 2)
		future = now_datetime() + timedelta(seconds=61)
		with (
			patch.object(jobs, "now_datetime", return_value=future),
			patch("medops.services.transactional_message.now_datetime", return_value=future),
		):
			receipt = self.claim(attempt=2)
		self.assertEqual(self.execute(receipt)["status"], "Posted")
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 2)

	def test_reversal_is_atomic_when_relevant_usage_is_unreconciled(self):
		self.assertEqual(self.execute(self.claim())["status"], "Posted")
		original_release = self.release
		original_rows = frappe.get_all(LEDGER, filters={"voucher_no": original_release}, fields=["*"])
		self.login_actor(source_fixture.MANAGER)
		self.release = self.success(
			self.post_command(
				"release.create_reversal",
				dict(
					release=original_release,
					expected_revision=3,
					posting_date="2026-10-27",
					reason="合成占用冲销保护",
				),
			)
		)["name"]
		self.authorize()
		receipt = self.claim()
		with patch.object(release_usage_guard, "_has_overlapping_usage", return_value=True):
			self.assert_failed_atomic(self.execute(receipt))
		self.assertEqual(
			original_rows, frappe.get_all(LEDGER, filters={"voucher_no": original_release}, fields=["*"])
		)

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

	def test_persisted_release_guard_detects_role_revocation_from_another_transaction(self):
		frappe.db.rollback()
		frappe.set_user(source_fixture.MANAGER)
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		revision = int(frappe.db.get_value(RELEASE, self.release, "revision"))
		original = control_release.prepare_release_guards
		calls = []

		def guard_then_revoke(*args, **kwargs):
			keys = original(*args, **kwargs)
			calls.append(1)
			primary = frappe.local.db
			with self.secondary_connection():
				frappe.set_user("Administrator")
				try:
					frappe.get_doc("User", source_fixture.MANAGER).remove_roles("HRP Budget Manager")
					self._secondary_connection.commit()
				except Exception:
					self._secondary_connection.rollback()
					raise
			self.assertIs(frappe.local.db, primary)
			frappe.set_user(source_fixture.MANAGER)
			return keys

		try:
			with patch.object(control_release, "prepare_release_guards", side_effect=guard_then_revoke):
				with self.assertRaises(IoneApplicationError):
					control_release.prepare_persisted_release_guards(self.release, revision)
			self.assertEqual(len(calls), 1)
			marker = getattr(frappe.local.db, "_medops_budget_guard_marker", None)
			assert marker is not None
			self.assertTrue(marker.state.invalidated)
			with self.assertRaises(IoneApplicationError):
				assert_budget_guards(marker.keys)
			self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 0)
		finally:
			frappe.db.rollback()
			frappe.set_user("Administrator")
			frappe.get_doc("User", source_fixture.MANAGER).add_roles("HRP Budget Manager")
			frappe.local.db.commit()
			frappe.cache.hdel("roles", source_fixture.MANAGER)

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

	def test_guarded_release_inventory_reconciles_posted_batches_and_physical_entries(self):
		self.assertEqual(self.execute(self.claim())["status"], "Posted")
		original_release = self.release
		self.login_actor(source_fixture.MANAGER)
		self.release = self.success(
			self.post_command(
				"release.create_reversal",
				dict(
					release=original_release,
					expected_revision=3,
					posting_date="2026-10-27",
					reason="合成预算台账核对",
				),
			)
		)["name"]
		self.authorize()
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		revision = int(frappe.db.get_value(RELEASE, self.release, "revision"))
		parent_queries = []
		unpatched_get_all = frappe.get_all

		def record_posted_parent_queries(doctype, *args, **kwargs):
			filters = kwargs.get("filters") or {}
			result = unpatched_get_all(doctype, *args, **kwargs)
			if doctype == RELEASE and filters.get("status") == "Posted" and kwargs.get("pluck") == "name":
				parent_queries.append((dict(filters), result))
			return result

		with (
			patch.object(control_inventory, "RELEASE_PARENT_PAGE_SIZE", 1),
			patch.object(frappe, "get_all", side_effect=record_posted_parent_queries),
		):
			keys, entries = control_release.prepare_persisted_release_inventory(self.release, revision)
		self.assertGreaterEqual(len(parent_queries), 2)
		self.assertEqual(parent_queries[1][0]["name"], [">", parent_queries[0][1][-1]])
		original_names = set(frappe.get_all(LEDGER, filters={"voucher_no": original_release}, pluck="name"))
		self.assertEqual(len(original_names), 2)
		self.assertTrue(original_names.issubset({entry.name for entry in entries}))
		self.assertEqual(
			{entry.key.organization_unit for entry in entries}, {self.units["A"], self.units["B"]}
		)
		for entry in entries:
			account, center = frappe.db.get_value(LEDGER, entry.name, ["account", "cost_center"])
			self.assertEqual((entry.account, entry.cost_center), (account, center))
		original_entry = next(entry for entry in entries if entry.name in original_names)
		chosen = original_entry.key

		def scope(key):
			return (
				key.company,
				key.hospital,
				key.organization_unit,
				key.budget_subject,
				key.funding_source,
				key.project,
			)

		related = [entry for entry in entries if scope(entry.key) == scope(chosen)]
		matches = match_purchase_release_keys(
			[chosen],
			related,
			company=chosen.company,
			hospital=chosen.hospital,
			organization_unit=chosen.organization_unit,
			project=chosen.project,
			account=original_entry.account,
			cost_center=original_entry.cost_center,
			currency=original_entry.currency,
			budget_date=chosen.period_start,
			amount="0.000000001",
		)
		self.assertEqual([candidate.key for candidate in matches], [chosen])
		original_get_all = frappe.get_all

		def hide_physical_entry(doctype, *args, **kwargs):
			result = original_get_all(doctype, *args, **kwargs)
			if doctype == LEDGER and kwargs.get("pluck") == "name" and result:
				return result[1:]
			return result

		with patch.object(frappe, "get_all", side_effect=hide_physical_entry):
			with self.assertRaises(IoneApplicationError):
				control_inventory.guarded_release_inventory(keys)
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards(keys)
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 0)
		frappe.db.rollback()

	def test_guarded_purchase_match_reads_real_posted_release_without_financial_writes(self):
		self.assertEqual(self.execute(self.claim())["status"], "Posted")
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		row_name = frappe.db.get_value(
			LEDGER, {"voucher_no": self.release, "organization_unit": self.units["A"]}, "name"
		)
		row = frappe.get_doc(LEDGER, row_name)
		if not frappe.db.exists("Item Group", "All Item Groups"):
			frappe.get_doc(dict(doctype="Item Group", item_group_name="All Item Groups", is_group=1)).insert(
				ignore_permissions=True
			)
		if not frappe.db.exists("Item Group", "COD034 Match Products"):
			frappe.get_doc(
				dict(
					doctype="Item Group",
					item_group_name="COD034 Match Products",
					parent_item_group="All Item Groups",
				)
			).insert(ignore_permissions=True)
		if not frappe.db.exists("UOM", "COD034 Match Unit"):
			frappe.get_doc(dict(doctype="UOM", uom_name="COD034 Match Unit")).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", "COD034-MATCH-ITEM"):
			frappe.get_doc(
				dict(
					doctype="Item",
					item_code="COD034-MATCH-ITEM",
					item_name="COD034 match fixture",
					item_group="COD034 Match Products",
					stock_uom="COD034 Match Unit",
					is_stock_item=0,
				)
			).insert(ignore_permissions=True)
		warehouse = frappe.db.get_value("Warehouse", {"company": row.company, "is_group": 0}, "name")
		request_date = str(row.period_start)
		schedule_date = add_days(request_date, 1)
		request = frappe.get_doc(
			dict(
				doctype="Material Request",
				company=row.company,
				material_request_type="Purchase",
				transaction_date=request_date,
				schedule_date=schedule_date,
				items=[
					dict(
						item_code="COD034-MATCH-ITEM",
						qty=1,
						uom="COD034 Match Unit",
						conversion_factor=1,
						warehouse=warehouse,
						cost_center=row.cost_center,
						expense_account=row.account,
						project=row.project,
						schedule_date=schedule_date,
						rate=1,
						amount=1,
					)
				],
			)
		).insert(ignore_permissions=True)
		source = locked_material_request_source(request.name, request.items[0].name, row.organization_unit)
		key_values = {
			field: str(row.get(field)) if field in {"period_start", "period_end"} else row.get(field)
			for field in KEY_FIELDS
		}
		before = {
			doctype: frappe.db.count(doctype)
			for doctype in (LEDGER, "HRP Budget Reservation", "HRP Budget Reservation Event")
		}
		result = prepare_purchase_release_match(
			request.name,
			request.items[0].name,
			row.organization_unit,
			expected_source_digest=source.digest(),
			budget_date=request_date,
			proposed_keys=[key_values],
		)
		self.assertEqual(len(result.candidates), 1)
		self.assertEqual(result.candidates[0].key.organization_unit, row.organization_unit)
		self.assertEqual(result.source.expense_account, row.account)
		self.assertEqual(result.source.cost_center, row.cost_center)
		self.assertEqual(
			guarded_material_reservation_observation(
				result.source, [result.candidates[0].key], allocations={}
			),
			(),
		)
		# Construct evidence in memory only: the closed event/aggregate tables
		# must remain unchanged while the real ERPNext source is checked.
		digest = result.source.digest()
		allocation = "synthetic-allocation-1"
		origin = ReservationSource(
			"Material Request", request.name, digest, request.items[0].name, allocation, "Reserve"
		)
		reservation = "synthetic-reservation-1"
		precision = result.candidates[0].precision
		event = ReservationEvent(
			reservation_event_key(reservation, 1),
			reservation,
			1,
			result.candidates[0].key,
			result.source.currency,
			precision,
			"Reserve",
			exact_decimal_amount("1", precision),
		)
		record = reservation_record(
			event,
			origin,
			actor="Administrator",
			occurred_at="2026-10-27 09:30:00.000000",
			previous_digest=None,
		)
		anchor = reservation_anchor(
			[record], expected_source=origin, source_snapshot_digest=digest, cost_center=source.cost_center
		)
		aggregate_row = reservation_anchor_row(anchor)
		event_rows = [reservation_record_row(record)]
		position = verify_material_reservation_evidence(
			aggregate_row, event_rows, source=result.source, allocation=allocation, expected_key=event.key
		)
		self.assertEqual(position.reserved, exact_decimal_amount("1", precision))
		with self.assertRaises(ValueError):
			verify_material_reservation_evidence(
				aggregate_row,
				event_rows,
				source=replace(result.source, amount="2.000000000"),
				allocation=allocation,
				expected_key=event.key,
			)
		with self.assertRaises(ValueError):
			verify_material_reservation_evidence(
				aggregate_row,
				event_rows,
				source=result.source,
				allocation="other-allocation",
				expected_key=event.key,
			)
		with self.assertRaises(ValueError):
			verify_material_reservation_evidence(
				aggregate_row,
				event_rows,
				source=result.source,
				allocation=allocation,
				expected_key=replace(event.key, funding_source="other-funding"),
			)
		oversized = reservation_record(
			replace(event, amount=exact_decimal_amount("2", precision)),
			origin,
			actor="Administrator",
			occurred_at="2026-10-27 09:30:00.000000",
			previous_digest=None,
		)
		oversized_anchor = reservation_anchor(
			[oversized], expected_source=origin, source_snapshot_digest=digest, cost_center=source.cost_center
		)
		with self.assertRaises(ValueError):
			verify_material_reservation_evidence(
				reservation_anchor_row(oversized_anchor),
				[reservation_record_row(oversized)],
				source=result.source,
				allocation=allocation,
				expected_key=event.key,
			)
		self.assertEqual(before, {doctype: frappe.db.count(doctype) for doctype in before})
		frappe.db.rollback()

	def test_real_posted_release_and_purchase_create_confirmation_evidence(self):
		self.exercise_real_release_and_confirmation(initial_write=False)

	def test_private_initial_reservation_writes_and_rolls_back_atomically(self):
		self.exercise_real_release_and_confirmation(initial_write=True)

	def exercise_real_release_and_confirmation(self, *, initial_write):
		"""No budget, source or evidence substitutes; all documents are synthetic."""
		self.assertEqual(self.execute(self.claim())["status"], "Posted")
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		row_name = frappe.db.get_value(
			LEDGER, {"voucher_no": self.release, "organization_unit": self.units["A"]}, "name"
		)
		row = frappe.get_doc(LEDGER, row_name)
		unique = uuid4().hex[:12]
		item_name = "COD035-EVIDENCE-" + unique
		unit_name = "COD035 Unit " + unique
		group_name = "COD035 Group " + unique
		created_root = not frappe.db.exists("Item Group", "All Item Groups")
		request_name = None
		primary = frappe.local.db
		previous_request = getattr(frappe.local, "request", None)
		previous_sid = frappe.session.sid
		temporary_tables = []
		try:
			if created_root:
				frappe.get_doc(
					dict(doctype="Item Group", item_group_name="All Item Groups", is_group=1)
				).insert(ignore_permissions=True)
			frappe.get_doc(
				dict(doctype="Item Group", item_group_name=group_name, parent_item_group="All Item Groups")
			).insert(ignore_permissions=True)
			frappe.get_doc(dict(doctype="UOM", uom_name=unit_name)).insert(ignore_permissions=True)
			frappe.get_doc(
				dict(
					doctype="Item",
					item_code=item_name,
					item_name="Synthetic budget evidence item",
					item_group=group_name,
					stock_uom=unit_name,
					is_stock_item=0,
				)
			).insert(ignore_permissions=True)
			request_date = str(row.period_start)
			schedule_date = add_days(request_date, 1)
			warehouse = frappe.db.get_value("Warehouse", {"company": row.company, "is_group": 0}, "name")
			request = frappe.get_doc(
				dict(
					doctype="Material Request",
					company=row.company,
					material_request_type="Purchase",
					transaction_date=request_date,
					schedule_date=schedule_date,
					items=[
						dict(
							item_code=item_name,
							qty=1,
							uom=unit_name,
							conversion_factor=1,
							warehouse=warehouse,
							cost_center=row.cost_center,
							expense_account=row.account,
							project=row.project,
							schedule_date=schedule_date,
							rate=1,
							amount=1,
						)
					],
				)
			).insert(ignore_permissions=True)
			request_name = request.name
			request.submit()
			# Publish only synthetic fixtures for the isolated test's owned transaction.
			primary.commit()
			source = locked_material_request_source(
				request.name, request.items[0].name, row.organization_unit
			)
			frappe.db.rollback()
			snapshot, digest = material_source_snapshot(asdict(source))
			values = {
				field: str(row.get(field)) if field in {"period_start", "period_end"} else row.get(field)
				for field in KEY_FIELDS
			}
			key = budget_control_key(values)
			item = reservation_intent(
				source_snapshot=snapshot,
				source_snapshot_digest=digest,
				allocation="synthetic-actual-release-allocation",
				key=key,
				currency=source.currency,
				precision=int(row.currency_precision),
				amount=exact_decimal_amount("1", int(row.currency_precision)),
				budget_date=request_date,
				reason="Synthetic integration test",
			)
			if initial_write:
				# Connection-private copies keep test evidence out of real ledgers.
				for table, template in (
					("tabHRP Budget Reservation Authorization", "cod035_authorization_template"),
					("tabHRP Budget Reservation", "cod035_reservation_template"),
					("tabHRP Budget Reservation Event", "cod035_event_template"),
				):
					primary.sql(
						f"CREATE TEMPORARY TABLE `{template}` LIKE `{table}`"  # nosec B608 - fixed test tables
					)
					try:
						primary.sql(
							f"CREATE TEMPORARY TABLE `{table}` LIKE `{template}`"  # nosec B608 - fixed test tables
						)
						temporary_tables.append(table)
					finally:
						primary.sql(f"DROP TEMPORARY TABLE `{template}`")  # nosec B608 - fixed test tables
			before = {
				doctype: frappe.db.count(doctype)
				for doctype in (LEDGER, "HRP Budget Reservation", "HRP Budget Reservation Event")
			}
			set_request(method="POST", path="/")
			frappe.session.sid = "synthetic-test-web-session"
			result = reservation_confirmation.prepare_reservation_confirmation(item)
			self.assertTrue(frappe.db.exists(reservation_confirmation.DOCTYPE, result["confirmation"]))
			assert_budget_guards([key])
			self.assertEqual(before, {doctype: frappe.db.count(doctype) for doctype in before})
			if not initial_write:
				frappe.db.rollback()
				self.assertFalse(frappe.db.exists(reservation_confirmation.DOCTYPE, result["confirmation"]))
			else:
				primary.commit()
				decision = reservation_confirmation.confirm_reservation_confirmation(
					result["confirmation"], result["proof"], True
				)
				primary.commit()
				with patch.dict(frappe.conf, {"medops_internal_reservation_write_enabled": False}):
					with self.assertRaises(IoneApplicationError):
						initial_reservation_write.append_initial_reservation(
							result["confirmation"], decision["decision"], result["proof"]
						)
					self.assertEqual(frappe.db.count("HRP Budget Reservation"), 0)
				with patch.dict(frappe.conf, {"medops_internal_reservation_write_enabled": True}):
					from medops.hrp_budget.doctype.hrp_budget_reservation_event.hrp_budget_reservation_event import (
						HRPBudgetReservationEvent,
					)

					with patch.object(
						HRPBudgetReservationEvent, "db_insert", side_effect=RuntimeError("synthetic failure")
					):
						with self.assertRaises(RuntimeError):
							initial_reservation_write.append_initial_reservation(
								result["confirmation"], decision["decision"], result["proof"]
							)
						with self.assertRaises(IoneApplicationError):
							assert_budget_guards([key])
					primary.rollback()
					self.assertEqual(frappe.db.count("HRP Budget Reservation"), 0)
					self.assertEqual(frappe.db.count("HRP Budget Reservation Event"), 0)
					with patch.object(
						initial_reservation_write,
						"_check_persisted_document",
						side_effect=RuntimeError("synthetic recheck failure"),
					):
						with self.assertRaises(RuntimeError):
							initial_reservation_write.append_initial_reservation(
								result["confirmation"], decision["decision"], result["proof"]
							)
						with self.assertRaises(IoneApplicationError):
							assert_budget_guards([key])
					primary.rollback()
					self.assertEqual(frappe.db.count("HRP Budget Reservation"), 0)
					self.assertEqual(frappe.db.count("HRP Budget Reservation Event"), 0)
					posted = initial_reservation_write.append_initial_reservation(
						result["confirmation"], decision["decision"], result["proof"]
					)
					self.assertEqual(
						frappe.db.get_value("HRP Budget Reservation", posted["reservation"], "docstatus"), 1
					)
					self.assertEqual(frappe.db.count("HRP Budget Reservation Event"), 1)
					self.assertEqual(frappe.db.count(LEDGER), before[LEDGER])
					primary.commit()
					with self.assertRaises(IoneApplicationError):
						initial_reservation_write.append_initial_reservation(
							result["confirmation"], decision["decision"], result["proof"]
						)
					primary.rollback()
					self.assertEqual(frappe.db.count("HRP Budget Reservation"), 1)
					self.assertEqual(frappe.db.count("HRP Budget Reservation Event"), 1)
		finally:
			frappe.db.rollback()
			frappe.local.request = previous_request
			frappe.session.sid = previous_sid
			for table in reversed(temporary_tables):
				primary.sql(f"DROP TEMPORARY TABLE `{table}`")  # nosec B608 - fixed test tables
			primary.rollback()
			if request_name and frappe.db.exists("Material Request", request_name):
				current = frappe.get_doc("Material Request", request_name)
				if current.docstatus == 1:
					current.cancel()
				frappe.delete_doc("Material Request", request_name, ignore_permissions=True)
			for doctype, name in (("Item", item_name), ("UOM", unit_name), ("Item Group", group_name)):
				if frappe.db.exists(doctype, name):
					frappe.delete_doc(doctype, name, ignore_permissions=True)
			if created_root and frappe.db.exists("Item Group", "All Item Groups"):
				frappe.delete_doc("Item Group", "All Item Groups", ignore_permissions=True)
			primary.commit()
