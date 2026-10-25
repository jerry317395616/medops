from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase
from frappe.utils import get_datetime, now_datetime

from medops.common.error_catalog import IoneApplicationError
from medops.common.sla import policy_payload
from medops.hrp_workflow_authorization.services.sla import (
	CASE,
	POLICY,
	TODO_PREFIX,
	CaseAction,
	UpsertPolicy,
	freeze_calendar,
	get_case,
	list_cases,
	notify_current_task,
	process_sla_case,
	run_due_sla_cases,
	sync_source,
)
from medops.hrp_workflow_authorization.tests.test_approval_matrix import (
	TEST_COMPANY,
	TEST_HOSPITAL,
	TEST_PLAIN_USER,
	TEST_ROOT,
	TEST_UNIT,
	TEST_USER,
	ensure_approval_fixtures,
)
from medops.setup.sla import ensure_sla_governance

SOURCE = "MedOps SLA Test Request"
API = "medops.api.v1.core.sla."


def make_deduplication_identifier(label: str) -> str:
	"""Public deterministic test identifiers, distinct from authentication credentials."""
	return label


def ensure_sla_fixtures() -> None:
	ensure_approval_fixtures()
	if not frappe.db.exists("DocType", SOURCE):
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": SOURCE,
				"module": "Custom",
				"custom": 1,
				"autoname": "hash",
				"fields": [
					{"fieldname": key, "label": key, "fieldtype": kind, **({"options": opts} if opts else {})}
					for key, kind, opts in [
						("company", "Link", "Company"),
						("hospital", "Link", "HRP Hospital"),
						("organization_unit", "Link", "HRP Organization Unit"),
						("status", "Select", "Draft\nPending\nOn Hold\nApproved"),
						("submitted_at", "Datetime", None),
						("responded_at", "Datetime", None),
					]
				],
				"permissions": [
					{"role": "System Manager", "read": 1, "write": 1, "create": 1},
					{"role": "HRP User", "read": 1},
				],
			}
		).insert(ignore_permissions=True)
	frappe.local.db.commit()


def reset_sla() -> None:
	frappe.set_user("Administrator")
	frappe.db.delete("ToDo", {"name": ("like", TODO_PREFIX + "%")})
	frappe.db.delete(CASE)
	frappe.db.delete(POLICY)
	frappe.db.delete(SOURCE)
	frappe.db.delete(
		"HRP Service Idempotency",
		{"service_name": ("in", [UpsertPolicy.definition.name, CaseAction.definition.name])},
	)
	frappe.db.delete("HRP Access Scope Member", {"parent": "COD028-SCOPE"})
	frappe.db.delete("HRP Access Scope", {"code": "COD028-SCOPE"})
	frappe.local.medops_sla_targets = None


def policy(**overrides):
	return policy_payload(
		{
			"code": "COD028-SLA",
			"display_name": "申请服务时限",
			"target_doctype": SOURCE,
			"company": TEST_COMPANY,
			"hospital": TEST_HOSPITAL,
			"organization_unit": TEST_UNIT,
			"valid_from": "2026-01-01",
			"status_field": "status",
			"start_field": "submitted_at",
			"response_field": "responded_at",
			"start_statuses_json": ["Pending"],
			"pause_statuses_json": ["On Hold"],
			"completion_statuses_json": ["Approved"],
			"response_seconds": 60,
			"resolution_seconds": 120,
			"clock_basis": "Elapsed",
			"responsible_user": "Administrator",
			"escalation_steps_json": [
				{"level": 1, "delay_seconds": 0, "assigned_to": "Administrator"},
				{"level": 2, "delay_seconds": 60, "assigned_to": "Administrator"},
			],
			**overrides,
		}
	)


def save_policy(payload=None, *, revision=0, key="create"):
	return UpsertPolicy().execute(
		{"policy": payload or policy(), "expected_revision": revision},
		idempotency_key=make_deduplication_identifier("COD028-policy-") + key,
	)


def create_source(*, at=None):
	stamp = at or now_datetime()
	with patch("medops.hrp_workflow_authorization.services.sla.now_datetime", return_value=stamp):
		return frappe.get_doc(
			{
				"doctype": SOURCE,
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"organization_unit": TEST_UNIT,
				"status": "Pending",
				"submitted_at": stamp,
			}
		).insert(ignore_permissions=True)


def case_for(source):
	return frappe.get_doc(CASE, {"reference_doctype": SOURCE, "reference_name": source.name})


def process(case, at, *, key="process"):
	with patch("medops.hrp_workflow_authorization.services.sla.now_datetime", return_value=at):
		return CaseAction().execute(
			{"case_name": case.name, "action": "process", "expected_revision": None, "reason": None},
			idempotency_key=make_deduplication_identifier("COD028-action-") + key,
		)


class TestSLA(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_sla_fixtures()

	def setUp(self):
		super().setUp()
		reset_sla()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_migration_indexes_and_service_only_writes(self):
		self.assertEqual(ensure_sla_governance(), ensure_sla_governance())
		indexes = {r.Key_name for r in frappe.db.sql("SHOW INDEX FROM `tabHRP SLA Escalation`", as_dict=True)}
		self.assertIn("idx_hrp_sla_due", indexes)
		with self.assertRaises(IoneApplicationError):
			frappe.get_doc({"doctype": POLICY, **policy()}).insert(ignore_permissions=True)

	def test_scheduler_dispatches_deduplicated_independent_case_jobs(self):
		save_policy()
		source = create_source()
		at = get_datetime(source.submitted_at) + timedelta(seconds=61)
		with (
			patch("medops.hrp_workflow_authorization.services.sla.now_datetime", return_value=at),
			patch("frappe.enqueue") as enqueue,
			patch("frappe.in_test", False),
		):
			self.assertEqual(run_due_sla_cases(), {"queued": 1})
		self.assertEqual(enqueue.call_args.kwargs["case_name"], case_for(source).name)
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])
		self.assertFalse(enqueue.call_args.kwargs["now"])

	def test_policy_replay_revision_immutability_and_snapshot(self):
		first = save_policy()
		self.assertTrue(save_policy().idempotency_replayed)
		source = create_source()
		case = case_for(source)
		save_policy(policy(response_seconds=90), revision=1, key="update")
		self.assertEqual(frappe.get_doc(POLICY, first.result["name"]).revision, 2)
		self.assertEqual(json.loads(case.snapshot_json)["policy"]["response_seconds"], 60)
		with self.assertRaises(IoneApplicationError):
			save_policy(policy(response_seconds=90), revision=1, key="stale")
		with self.assertRaises(IoneApplicationError):
			save_policy(policy(organization_unit=TEST_ROOT), revision=2, key="identity")

	def test_repeated_source_hooks_and_dispatch_do_not_duplicate_tasks_or_share(self):
		save_policy()
		source = create_source()
		case = case_for(source)
		at = get_datetime(source.submitted_at) + timedelta(seconds=61)
		before_shares = frappe.db.count("DocShare")
		first = process(case, at)
		self.assertTrue(process(case, at).idempotency_replayed)
		process(case, at, key="second-key")
		with patch("medops.hrp_workflow_authorization.services.sla.now_datetime", return_value=at):
			sync_source(source)
		self.assertEqual(frappe.db.count(CASE), 1)
		self.assertEqual(frappe.db.count("ToDo", {"name": ("like", TODO_PREFIX + "%")}), 1)
		self.assertEqual(first.result["response_level"], 1)
		self.assertEqual(frappe.db.count("DocShare"), before_shares)

	def test_pause_resume_preserves_fractional_elapsed_and_breach_age(self):
		save_policy()
		source = create_source()
		start = get_datetime(source.submitted_at)
		with patch(
			"medops.hrp_workflow_authorization.services.sla.now_datetime",
			return_value=start + timedelta(seconds=30.25),
		):
			source.status = "On Hold"
			source.save(ignore_permissions=True)
		case = case_for(source)
		self.assertEqual(case.status, "Paused")
		self.assertFalse(case.next_run_at)
		self.assertAlmostEqual(case.elapsed_work_seconds, 30.25)
		process(case, start + timedelta(hours=2), key="paused")
		self.assertFalse(case_for(source).response_breached)
		with patch(
			"medops.hrp_workflow_authorization.services.sla.now_datetime",
			return_value=start + timedelta(hours=3),
		):
			source.status = "Pending"
			source.save(ignore_permissions=True)
		case = case_for(source)
		self.assertEqual(get_datetime(case.response_due_at), start + timedelta(hours=3, seconds=29.75))
		process(case, start + timedelta(hours=3, seconds=90), key="resumed")
		self.assertEqual(case_for(source).response_level, 2)

	def test_completion_closes_tasks_and_never_restarts_same_anchor(self):
		save_policy()
		source = create_source()
		at = get_datetime(source.submitted_at) + timedelta(seconds=130)
		process(case_for(source), at)
		with patch(
			"medops.hrp_workflow_authorization.services.sla.now_datetime",
			return_value=at + timedelta(seconds=1),
		):
			source.status = "Approved"
			source.save(ignore_permissions=True)
			self.assertEqual(case_for(source).status, "Completed")
			source.status = "Pending"
			source.save(ignore_permissions=True)
		self.assertEqual(frappe.db.count(CASE), 1)
		self.assertFalse(frappe.db.count("ToDo", {"name": ("like", TODO_PREFIX + "%"), "status": "Open"}))

	def test_new_anchor_cancels_old_cycle_and_starts_new_one(self):
		save_policy()
		source = create_source()
		case = case_for(source)
		at = get_datetime(source.submitted_at) + timedelta(minutes=2)
		with patch("medops.hrp_workflow_authorization.services.sla.now_datetime", return_value=at):
			source.submitted_at = at
			source.save(ignore_permissions=True)
		self.assertEqual(frappe.db.count(CASE), 2)
		self.assertEqual(frappe.get_doc(CASE, case.name).status, "Cancelled")
		self.assertEqual(frappe.db.count(CASE, {"status": "Open"}), 1)

	def test_acknowledge_replay_and_manual_cancel_require_revision(self):
		save_policy()
		source = create_source()
		case = case_for(source)
		cmd = {
			"case_name": case.name,
			"action": "acknowledge",
			"expected_revision": case.revision,
			"reason": None,
		}
		CaseAction().execute(cmd, idempotency_key=make_deduplication_identifier("COD028-acknowledge"))
		self.assertTrue(
			CaseAction()
			.execute(cmd, idempotency_key=make_deduplication_identifier("COD028-acknowledge"))
			.idempotency_replayed
		)
		with self.assertRaises(IoneApplicationError):
			CaseAction().execute(
				{**cmd, "action": "cancel", "reason": "测试停止"},
				idempotency_key=make_deduplication_identifier("COD028-stale-cancel"),
			)
		case = case_for(source)
		CaseAction().execute(
			{**cmd, "action": "cancel", "expected_revision": case.revision, "reason": "测试停止"},
			idempotency_key=make_deduplication_identifier("COD028-cancel"),
		)
		self.assertEqual(case_for(source).status, "Cancelled")

	def test_recipient_without_source_scope_fails_without_autoshare_and_retries(self):
		save_policy(
			policy(escalation_steps_json=[{"level": 1, "delay_seconds": 0, "assigned_to": TEST_USER}])
		)
		source = create_source()
		case = case_for(source)
		before = frappe.db.count("DocShare")
		with patch(
			"medops.hrp_workflow_authorization.services.sla.now_datetime",
			return_value=get_datetime(source.submitted_at) + timedelta(seconds=61),
		):
			result = process_sla_case(case.name)
		self.assertEqual(result, {"processed": 0, "failed": 1})
		case.reload()
		self.assertEqual(case.retry_count, 1)
		self.assertTrue(case.last_error_code)
		self.assertEqual(case.response_level, 0)
		self.assertEqual(frappe.db.count("DocShare"), before)
		self.assertFalse(frappe.db.count("ToDo", {"name": ("like", TODO_PREFIX + "%")}))

	def test_cross_scope_read_list_ack_and_direct_todo_tamper_are_denied(self):
		save_policy()
		source = create_source()
		case = case_for(source)
		process(case, get_datetime(source.submitted_at) + timedelta(seconds=61))
		name = next(iter(json.loads(case_for(source).current_assignments_json).values()))
		todo = frappe.get_doc("ToDo", name)
		todo.allocated_to = TEST_USER
		with self.assertRaises(IoneApplicationError):
			todo.save(ignore_permissions=True)
		frappe.set_user(TEST_PLAIN_USER)
		with self.assertRaises(IoneApplicationError):
			get_case(case.name)
		self.assertEqual(list_cases()["items"], [])
		with self.assertRaises(IoneApplicationError):
			CaseAction().execute(
				{
					"case_name": case.name,
					"action": "acknowledge",
					"expected_revision": case.revision,
					"reason": None,
				},
				idempotency_key=make_deduplication_identifier("COD028-denied-ack"),
			)

	def test_standard_todo_close_does_not_acknowledge_or_complete_source(self):
		save_policy()
		source = create_source()
		case = case_for(source)
		process(case, get_datetime(source.submitted_at) + timedelta(seconds=61))
		name = next(iter(json.loads(case_for(source).current_assignments_json).values()))
		todo = frappe.get_doc("ToDo", name)
		todo.status = "Closed"
		todo.save(ignore_permissions=True)
		case.reload()
		self.assertEqual(case.status, "Open")
		self.assertFalse(case.responded_at)

	def test_notification_worker_rechecks_durable_task_and_deduplicates(self):
		save_policy()
		source = create_source()
		case = case_for(source)
		process(case, get_datetime(source.submitted_at) + timedelta(seconds=61))
		case.reload()
		name = json.loads(case.current_assignments_json)["response"]
		filters = {"link": f"/desk/todo/{name}", "for_user": "Administrator"}
		before = frappe.db.count("Notification Log", filters)
		self.assertEqual(before, 1)
		self.assertTrue(notify_current_task(case.name, name, "response"))
		self.assertEqual(frappe.db.count("Notification Log", filters), before)
		self.assertFalse(notify_current_task(case.name, TODO_PREFIX + "rolled-back", "response"))
		todo = frappe.get_doc("ToDo", name)
		todo.status = "Closed"
		todo.save(ignore_permissions=True)
		self.assertFalse(notify_current_task(case.name, name, "response"))

	def test_specific_policy_wins_and_ambiguous_equal_rank_fails(self):
		save_policy(policy(code="COD028-GLOBAL", organization_unit=None), key="global")
		save_policy()
		source = create_source()
		self.assertEqual(case_for(source).sla_policy, "COD028-SLA")
		save_policy(policy(code="COD028-TIE"), key="tie")
		with self.assertRaises(IoneApplicationError):
			create_source()

	def test_frozen_holiday_calendar_uses_standard_erpnext_hours(self):
		doc = frappe.get_doc(
			{
				"doctype": "Holiday List",
				"holiday_list_name": "COD028-Holidays",
				"from_date": "2026-01-01",
				"to_date": "2026-12-31",
				"holidays": [{"holiday_date": "2026-10-26", "description": "测试节假日"}],
			}
		).insert(ignore_permissions=True)
		payload = policy(
			clock_basis="Business Hours",
			holiday_list=doc.name,
			support_hours_json=[{"workday": "Monday", "start_time": "08:00:00", "end_time": "17:00:00"}],
		)
		calendar = freeze_calendar(payload)
		self.assertEqual(calendar["days"]["Monday"], [28800, 61200])
		self.assertEqual(calendar["holidays"], ["2026-10-26"])
		frappe.db.set_value("Holiday", doc.holidays[0].name, "holiday_date", "2026-10-27")
		self.assertEqual(calendar["holidays"], ["2026-10-26"])

	def test_expired_calendar_does_not_block_business_completion(self):
		from medops.common.sla import WEEKDAYS

		frappe.get_doc(
			{
				"doctype": "Holiday List",
				"holiday_list_name": "COD028-Expiry",
				"from_date": "2026-01-01",
				"to_date": "2026-12-31",
			}
		).insert(ignore_permissions=True)
		save_policy(
			policy(
				clock_basis="Business Hours",
				holiday_list="COD028-Expiry",
				support_hours_json=[
					{"workday": day, "start_time": "08:00:00", "end_time": "17:00:00"} for day in WEEKDAYS
				],
			)
		)
		source = create_source(at=get_datetime("2026-12-31 08:00:00"))
		with patch(
			"medops.hrp_workflow_authorization.services.sla.now_datetime",
			return_value=get_datetime("2027-01-01 09:00:00"),
		):
			source.status = "Approved"
			source.save(ignore_permissions=True)
		case = case_for(source)
		self.assertEqual(case.status, "Completed")
		self.assertEqual(case.last_error_code, "SLA-CLOCK-UNAVAILABLE")
		self.assertFalse(case.next_run_at)


class TestSLAAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_sla_fixtures()

	def setUp(self):
		super().setUp()
		reset_sla()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def test_http_upsert_replay_and_missing_idempotency(self):
		payload = {"policy": policy()}
		missing = self.post(self.method(API + "upsert_policy"), payload)
		self.assertEqual(missing.status_code, 400, missing.get_data(as_text=True))
		first = self.post(
			self.method(API + "upsert_policy"), payload, headers={"Idempotency-Key": "COD028-http-policy"}
		)
		self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
		repeat = self.post(
			self.method(API + "upsert_policy"), payload, headers={"Idempotency-Key": "COD028-http-policy"}
		)
		self.assertEqual(repeat.status_code, 200, repeat.get_data(as_text=True))
		self.assertTrue(repeat.get_json()["message"]["idempotency_replayed"])

	def test_http_unknown_arguments_and_invalid_pagination(self):
		for method, args in [
			("get", {"case_name": "missing", "unexpected": 1}),
			("list_my", {"limit": 51}),
			("list_my", {"offset": -1}),
		]:
			response = self.get(self.method(API + method), args)
			self.assertEqual(response.status_code, 400, response.get_data(as_text=True))

	def test_http_guest_rejected_for_all_endpoints(self):
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		for method in ("get", "list_my"):
			self.assertEqual(self.get(self.method(API + method)).status_code, 401)
		for method in ("act", "upsert_policy"):
			self.assertEqual(self.post(self.method(API + method), {}).status_code, 401)

	def test_http_get_list_and_acknowledgement_replay(self):
		save_policy()
		source = create_source()
		case = case_for(source)
		frappe.local.db.commit()
		read = self.get(self.method(API + "get"), {"case_name": case.name})
		self.assertEqual(read.status_code, 200, read.get_data(as_text=True))
		listing = self.get(self.method(API + "list_my"), {})
		self.assertEqual(listing.status_code, 200, listing.get_data(as_text=True))
		self.assertIn(case.name, [item["name"] for item in listing.get_json()["message"]["items"]])
		args = {"case_name": case.name, "action": "acknowledge", "expected_revision": case.revision}
		for replayed in (False, True):
			response = self.post(
				self.method(API + "act"), args, headers={"Idempotency-Key": "COD028-http-ack"}
			)
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
			self.assertEqual(response.get_json()["message"]["idempotency_replayed"], replayed)
		self.assertEqual(frappe.db.get_value(SOURCE, source.name, "status"), "Pending")
		self.assertEqual(self.get(self.method(API + "get"), {"case_name": "missing"}).status_code, 404)

	def test_http_authenticated_user_without_source_scope_is_denied(self):
		from frappe.auth import CookieManager, LoginManager
		from frappe.utils import set_request

		save_policy()
		source = create_source()
		case = case_for(source)
		frappe.local.db.commit()
		original_request = getattr(frappe.local, "request", None)
		set_request(path="/")
		try:
			frappe.local.cookie_manager = CookieManager()
			frappe.local.login_manager = LoginManager()
			frappe.local.login_manager.login_as(TEST_USER)
			user_sid = frappe.session.sid
		finally:
			frappe.local.request = original_request
			frappe.set_user("Administrator")
		self.TEST_CLIENT.set_cookie(key="sid", value=user_sid)
		response = self.get(self.method(API + "get"), {"case_name": case.name})
		self.assertEqual(response.status_code, 403, response.get_data(as_text=True))
		response = self.post(
			self.method(API + "act"),
			{"case_name": case.name, "action": "acknowledge", "expected_revision": case.revision},
			headers={"Idempotency-Key": "COD028-http-denied"},
		)
		self.assertEqual(response.status_code, 403, response.get_data(as_text=True))
		response = self.get(self.method(API + "list_my"), {})
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		self.assertEqual(response.get_json()["message"]["items"], [])
