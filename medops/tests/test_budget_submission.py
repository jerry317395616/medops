from __future__ import annotations

from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.budget_approval import SUBMISSION, SUBMISSION_LINE, build_department_submission
from medops.common.budget_planning import PLAN, build_action, build_version
from medops.common.domain_service import idempotency_record_name
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services.link_titles import get_link_title
from medops.hrp_budget.services.planning import freeze_budget_version, save_budget_plan, save_budget_version
from medops.hrp_budget.services.submission import (
	list_submissions,
	read_submission,
	seal_department_submission,
)
from medops.hrp_budget.services.submission_access import get_submission
from medops.setup.budget_submission import ensure_budget_submission
from medops.tests.test_budget_planning import (
	COMPANY,
	HOSPITAL,
	PLANNER,
	YEAR,
	create_budget_masters,
	ensure_budget_fixtures,
	key,
	plan_command,
)

PREFIX = "medops.api.v1.finance.submission."


def fixture_request(label: str) -> str:
	return "COD032S-" + label + "-" + uuid4().hex


def new_frozen_plan(org, unit, value, fund, *, amount="0.10", freeze=True):
	version = save_budget_version(
		build_version(
			{
				"company": COMPANY,
				"hospital": HOSPITAL,
				"organization_version": org,
				"fiscal_year": YEAR,
				"subject": "COD032科室申报测试",
			}
		),
		idempotency_key=fixture_request("version"),
	)
	plan = save_budget_plan(
		plan_command(version["name"], unit, value, fund, amount=amount),
		idempotency_key=fixture_request("plan"),
	)
	if freeze:
		freeze_budget_version(
			build_action({"name": version["name"], "expected_revision": version["revision"]}),
			idempotency_key=fixture_request("freeze"),
		)
	return version, plan


def submission_command(plan):
	return build_department_submission({"source_plan": plan["name"], "expected_revision": plan["revision"]})


class TestBudgetSubmissionServices(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.org, cls.unit = ensure_budget_fixtures()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.savepoint("cod032_submission_case")
		self.dimension, self.value, self.fund = create_budget_masters()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="cod032_submission_case")
		frappe.clear_cache()
		super().tearDown()

	def new_plan(self, **kwargs):
		return new_frozen_plan(self.org, self.unit, self.value["name"], self.fund["name"], **kwargs)

	def seal(self, plan, label="seal"):
		return seal_department_submission(submission_command(plan), idempotency_key="COD032S-" + label)

	def test_exact_snapshot_replay_and_unique_plan_do_not_post_financials(self):
		frappe.db.set_default("currency_precision", "9")
		before = {dt: frappe.db.count(dt) for dt in ("Budget", "GL Entry", "Stock Ledger Entry")}
		_, plan = self.new_plan(amount="999999999999.000000001")
		frappe.set_user(PLANNER)
		result = self.seal(plan)
		self.assertEqual(result["docstatus"], 1)
		item = read_submission(result["name"])
		self.assertEqual(item["status"], "Sealed")
		self.assertEqual(item["total_amount"], "999999999999.000000001")
		self.assertEqual(item["lines"][0]["origin_plan"], plan["name"])
		self.assertEqual(item["lines"][0]["funding_source"], self.fund["name"])
		self.assertNotIn("source_version_digest", item)
		self.assertNotIn("origin_version_digest", item["lines"][0])
		self.assertTrue(self.seal(plan)["idempotency_replayed"])
		with self.assertRaises(IoneApplicationError):
			self.seal(plan, "different-key")
		frappe.set_user("Administrator")
		self.assertEqual(before, {dt: frappe.db.count(dt) for dt in before})
		self.assertEqual(frappe.db.count(SUBMISSION, {"source_plan": plan["name"]}), 1)

	def test_unfrozen_and_stale_revision_fail_without_sealing(self):
		version, plan = self.new_plan(freeze=False)
		before = frappe.db.count(SUBMISSION)
		with self.assertRaises(IoneApplicationError):
			self.seal(plan)
		freeze_budget_version(
			build_action({"name": version["name"], "expected_revision": 1}),
			idempotency_key="COD032S-later-freeze",
		)
		with self.assertRaises(IoneApplicationError):
			self.seal({**plan, "revision": plan["revision"] + 1})
		self.assertEqual(frappe.db.count(SUBMISSION), before)

	def test_frozen_integrity_rejects_changed_source_and_keeps_no_receipt(self):
		_, plan = self.new_plan()
		frappe.db.set_value(PLAN, plan["name"], "subject", "tampered frozen fact")
		before = frappe.db.count(SUBMISSION)
		with self.assertRaises(IoneApplicationError) as error:
			self.seal(plan)
		self.assertEqual(error.exception.http_status_code, 409)
		self.assertEqual(frappe.db.count(SUBMISSION), before)
		self.assertFalse(
			frappe.db.exists(
				"HRP Service Idempotency",
				idempotency_record_name("hrp_budget.submission.seal_department", "COD032S-seal"),
			)
		)

	def test_direct_save_cancel_rename_and_submitted_edits_are_denied(self):
		_, plan = self.new_plan()
		result = self.seal(plan)
		for operation in (
			lambda: get_submission(result["name"]).save(ignore_permissions=True),
			lambda: get_submission(result["name"]).cancel(),
			lambda: frappe.rename_doc(SUBMISSION, result["name"], "COD032S-renamed", force=True),
		):
			with self.assertRaises((IoneApplicationError, frappe.PermissionError)):
				operation()
		doc = get_submission(result["name"])
		doc.flags.ignore_validate_update_after_submit = True
		doc.total_amount = "1.00"
		with self.assertRaises(IoneApplicationError):
			doc.save(ignore_permissions=True)

	def test_source_share_revocation_denies_snapshot_title_list_and_replay(self):
		_, plan = self.new_plan()
		frappe.set_user(PLANNER)
		result = self.seal(plan)
		frappe.set_user("Administrator")
		frappe.share.remove("HRP Hospital", HOSPITAL, PLANNER)
		frappe.set_user(PLANNER)
		for operation in (
			lambda: read_submission(result["name"]),
			lambda: get_link_title(SUBMISSION, result["name"]),
			lambda: self.seal(plan),
		):
			with self.assertRaises(IoneApplicationError):
				operation()
		self.assertEqual(list_submissions({"source_plan": plan["name"]})["items"], [])

	def test_source_and_submission_field_restrictions_fail_closed(self):
		_, plan = self.new_plan()
		frappe.set_user(PLANNER)
		result = self.seal(plan)
		frappe.set_user("Administrator")
		for doctype, field in ((PLAN, "total_amount"), (SUBMISSION, "total_amount")):
			meta = frappe.get_meta(doctype)
			row = meta.get_field(field)
			original = row.permlevel
			try:
				row.permlevel = 1
				frappe.set_user(PLANNER)
				with self.assertRaises(IoneApplicationError):
					read_submission(result["name"])
				self.assertEqual(list_submissions({"source_plan": plan["name"]})["items"], [])
			finally:
				frappe.set_user("Administrator")
				row.permlevel = original

	def test_friendly_labels_respect_current_source_field_masking(self):
		_, plan = self.new_plan()
		frappe.set_user(PLANNER)
		result = self.seal(plan)
		item = read_submission(result["name"])
		self.assertEqual(item["display_labels"]["budget_subject"][self.value["name"]], "设备预算")
		self.assertEqual(item["display_labels"]["funding_source"][self.fund["name"]], "自有资金")
		frappe.set_user("Administrator")
		field = frappe.get_meta("HRP Organization Unit").get_field("display_name")
		before = field.get("mask")
		try:
			field.mask = 1
			frappe.set_user(PLANNER)
			masked = read_submission(result["name"])
			self.assertNotIn(self.unit, masked["display_labels"]["organization_unit"])
			self.assertEqual(masked["snapshot_digest"], item["snapshot_digest"])
		finally:
			frappe.set_user("Administrator")
			field.mask = before

	def test_scope_revocation_and_generic_list_do_not_leak_snapshots(self):
		_, plan = self.new_plan()
		frappe.set_user(PLANNER)
		result = self.seal(plan)
		self.assertEqual(frappe.get_list(SUBMISSION, pluck="name"), [])
		with self.assertRaises(frappe.PermissionError):
			frappe.get_list(SUBMISSION_LINE, pluck="name")
		frappe.set_user("Administrator")
		frappe.db.set_value("HRP Access Scope", key("scope-" + PLANNER.split("@")[0]), "enabled", 0)
		frappe.set_user(PLANNER)
		with self.assertRaises(IoneApplicationError):
			read_submission(result["name"])
		with self.assertRaises(IoneApplicationError):
			self.seal(plan)
		self.assertEqual(list_submissions({"source_plan": plan["name"]})["items"], [])

	def test_repeat_setup_has_no_grants_and_native_writes_stay_disabled(self):
		before = {dt: frappe.db.count(dt) for dt in ("Has Role", "HRP Access Scope", "DocShare", SUBMISSION)}
		self.assertEqual(ensure_budget_submission(), ensure_budget_submission())
		self.assertEqual(before, {dt: frappe.db.count(dt) for dt in before})
		for dt in (SUBMISSION, SUBMISSION_LINE):
			meta = frappe.get_meta(dt)
			self.assertFalse(meta.allow_rename)
			self.assertFalse(
				any(p.create or p.write or p.submit or p.cancel or p.delete for p in meta.permissions)
			)
		self.assertEqual(frappe.get_meta(SUBMISSION).get_field("total_amount").fieldtype, "Data")
		self.assertEqual(frappe.get_meta(SUBMISSION_LINE).get_field("amount").fieldtype, "Data")

	def test_hospital_fence_prevents_concurrent_native_source_rename(self):
		_, plan = self.new_plan()
		self.seal(plan)
		primary = frappe.local.db
		try:
			with self.secondary_connection():
				with self.assertRaises(frappe.QueryTimeoutError):
					frappe.db.sql(
						"SELECT name FROM `tabHRP Hospital` WHERE name=%s FOR UPDATE NOWAIT", (HOSPITAL,)
					)
		finally:
			frappe.local.db = primary


class TestBudgetSubmissionAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.org, cls.unit = ensure_budget_fixtures()
		cls.dimension, cls.value, cls.fund = create_budget_masters()
		frappe.local.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)
		self.budget_version, self.plan = new_frozen_plan(
			self.org, self.unit, self.value["name"], self.fund["name"]
		)
		frappe.local.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("HRP Access Scope", key("scope-" + PLANNER.split("@")[0]), "enabled", 1)
		frappe.local.db.commit()
		super().tearDown()

	def login_planner(self):
		from frappe.auth import CookieManager, LoginManager
		from frappe.utils import set_request

		original = getattr(frappe.local, "request", None)
		set_request(path="/")
		try:
			frappe.local.cookie_manager = CookieManager()
			frappe.local.login_manager = LoginManager()
			frappe.local.login_manager.login_as(PLANNER)
			sid = frappe.session.sid
		finally:
			frappe.local.request = original
			frappe.set_user("Administrator")
		self.TEST_CLIENT.set_cookie(key="sid", value=sid)

	def seal(self, payload=None):
		return self.post(
			self.method(PREFIX + "seal_department"),
			{
				"payload": payload
				if payload is not None
				else {"source_plan": self.plan["name"], "expected_revision": 1}
			},
			headers={"Idempotency-Key": "COD032S-http-" + self._testMethodName + self.plan["name"][:16]},
		)

	def test_http_sealing_replay_read_and_native_field_levels(self):
		self.login_planner()
		response = self.seal()
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		name = response.get_json()["message"]["name"]
		self.assertNotIn("total_amount", response.get_json()["message"])
		self.assertTrue(self.seal().get_json()["message"]["idempotency_replayed"])
		read = self.get(self.method(PREFIX + "get_record"), {"name": name})
		self.assertEqual(read.status_code, 200, read.get_data(as_text=True))
		self.assertEqual(read.get_json()["message"]["total_amount"], "0.10")
		self.assertNotIn("source_version_digest", read.get_json()["message"])
		native = self.get(self.method("frappe.desk.form.load.getdoc"), {"doctype": SUBMISSION, "name": name})
		self.assertEqual(native.status_code, 200, native.get_data(as_text=True))
		doc = native.get_json()["docs"][0]
		self.assertFalse(doc.get("source_version_digest"))
		self.assertFalse(doc["lines"][0].get("origin_version_digest"))
		self.assertTrue(
			self.get(
				self.method(PREFIX + "list_records"), {"filters": {"source_plan": self.plan["name"]}}
			).get_json()["message"]["items"]
		)

	def test_http_guest_and_structured_filter_or_unknown_amount_are_denied(self):
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		self.assertEqual(self.seal([]).status_code, 401)
		for method in ("get_record", "list_records"):
			self.assertEqual(self.get(self.method(PREFIX + method), {"name": []}).status_code, 401)
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)
		self.assertEqual(
			self.seal(
				{"source_plan": self.plan["name"], "expected_revision": 1, "total_amount": "1"}
			).status_code,
			400,
		)
		self.assertEqual(
			self.get(
				self.method(PREFIX + "list_records"), {"filters": {"source_plan": ["like", "%"]}}
			).status_code,
			400,
		)

	def test_http_current_scope_denies_reads_lists_titles_and_durable_replay(self):
		self.login_planner()
		response = self.seal()
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		name = response.get_json()["message"]["name"]
		frappe.db.set_value("HRP Access Scope", key("scope-" + PLANNER.split("@")[0]), "enabled", 0)
		frappe.local.db.commit()
		self.assertEqual(self.get(self.method(PREFIX + "get_record"), {"name": name}).status_code, 403)
		self.assertEqual(self.seal().status_code, 403)
		self.assertEqual(
			self.get(
				self.method("frappe.desk.search.get_link_title"), {"doctype": SUBMISSION, "docname": name}
			).status_code,
			403,
		)
		listed = self.get(
			self.method(PREFIX + "list_records"), {"filters": {"source_plan": self.plan["name"]}}
		)
		self.assertEqual(listed.get_json()["message"], {"items": [], "limit": 50, "next_offset": None})
