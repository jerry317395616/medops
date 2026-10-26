from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests.test_api import FrappeAPITestCase
from frappe.utils import now_datetime

from medops.common.approval_matrix import build_approval_matrix_steps, build_approval_matrix_upsert
from medops.common.budget_workflow import APPROVER_ROLE, CASE, REVIEW, WORKFLOW, WORKFLOW_ACTIONS
from medops.common.domain_service import fingerprint_json
from medops.common.error_catalog import IoneApplicationError
from medops.common.segregation import build_segregation_rule_upsert
from medops.hrp_budget.services.approval_evidence import evidence_values
from medops.hrp_budget.services.approval_native import require_workflow_integrity
from medops.hrp_budget.services.submission import seal_department_submission
from medops.hrp_workflow_authorization.services.approval_matrix import upsert_approval_matrix
from medops.hrp_workflow_authorization.services.segregation import upsert_segregation_rule
from medops.setup.budget_approval import ensure_budget_approval
from medops.tests.scope_fixtures import grant_test_scope, share_test_records
from medops.tests.test_budget_planning import (
	COMPANY,
	HOSPITAL,
	PLANNER,
	YEAR,
	create_budget_masters,
	ensure_budget_fixtures,
)
from medops.tests.test_budget_submission import (
	fixture_request,
	new_frozen_plan,
	submission_command,
)

PREFIX = "medops.api.v1.finance.approval."
ACTORS = ("cod032-reviewer-one@example.com", "cod032-reviewer-two@example.com")


def ensure_workflow_fixtures():
	org, unit = ensure_budget_fixtures()
	ensure_budget_approval()
	for email in ACTORS:
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				dict(
					doctype="User",
					email=email,
					first_name="Synthetic Budget Reviewer",
					enabled=1,
					user_type="System User",
					send_welcome_email=0,
					roles=[{"role": role} for role in ("HRP User", APPROVER_ROLE, "Accounts User")],
				)
			).insert(ignore_permissions=True)
		grant_test_scope(
			code="COD032W-scope-" + email.split("@")[0], company=COMPANY, hospital=HOSPITAL, users=(email,)
		)
	for doctype, names in (
		("HRP Hospital", (HOSPITAL,)),
		("HRP Organization Version", (org,)),
		("HRP Organization Unit", (unit,)),
		(
			"HRP Organization Mapping",
			(frappe.db.get_value("HRP Organization Mapping", {"organization_unit": unit}, "name"),),
		),
	):
		share_test_records(doctype, tuple(str(name) for name in names), ACTORS)
	frappe.local.db.commit()
	return org, unit


class TestBudgetWorkflowAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.org, cls.unit = ensure_workflow_fixtures()
		cls.dimension, cls.value, cls.fund = create_budget_masters()
		frappe.local.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		# Each HTTP test owns its configuration; previously committed evidence stays immutable.
		frappe.db.set_value("HRP Approval Matrix", {"target_doctype": CASE, "company": COMPANY}, "enabled", 0)
		frappe.db.set_value(
			"HRP Segregation Rule", {"target_doctype": CASE, "company": COMPANY}, "enabled", 0
		)
		_, self.plan = new_frozen_plan(self.org, self.unit, self.value["name"], self.fund["name"])
		frappe.set_user(PLANNER)
		self.submission = seal_department_submission(
			submission_command(self.plan), idempotency_key=fixture_request("workflow-seal")
		)
		frappe.set_user("Administrator")
		self.matrix = self.configure()
		self.rule = upsert_segregation_rule(
			build_segregation_rule_upsert(
				code=fixture_request("sod"),
				display_name="预算制单复核分离",
				target_doctype=CASE,
				target_action="approve",
				company=COMPANY,
				hospital=HOSPITAL,
				organization_unit=self.unit,
				enabled=True,
				valid_from="2026-01-01",
				actor_fields=("applicant", "source_applicant", "requested_by"),
				conflicting_roles=(),
			),
			idempotency_key=fixture_request("sod-save"),
		)
		frappe.local.db.commit()
		self.login_actor(PLANNER)

	def configure(self, *, mode="All", sequential=False):
		if hasattr(self, "matrix"):
			frappe.db.set_value("HRP Approval Matrix", self.matrix["name"], "enabled", 0)
		steps = [
			dict(
				sequence_no=index if sequential else 1,
				step_name="财务复核" + str(index) if sequential else "财务复核",
				threshold_amount="0.00",
				approver_type="User",
				approver_user=user,
				approval_mode=mode,
			)
			for index, user in enumerate(ACTORS, 1)
		]
		return upsert_approval_matrix(
			build_approval_matrix_upsert(
				code=fixture_request("matrix"),
				display_name="预算独立复核",
				target_doctype=CASE,
				company=COMPANY,
				hospital=HOSPITAL,
				organization_unit=self.unit,
				valid_from="2026-01-01",
				priority=50,
				dimensions={"fiscal_year": YEAR, "currency": "CNY"},
				schema_version=2,
				amount_precision=2,
				steps=build_approval_matrix_steps(steps, schema_version=2, amount_precision=2),
			),
			idempotency_key=fixture_request("matrix-save"),
		)

	def login_actor(self, user):
		from frappe.auth import CookieManager, LoginManager
		from frappe.utils import set_request

		original = getattr(frappe.local, "request", None)
		set_request(path="/")
		try:
			frappe.local.cookie_manager = CookieManager()
			frappe.local.login_manager = LoginManager()
			frappe.local.login_manager.login_as(user)
			sid = frappe.session.sid
		finally:
			frappe.local.request = original
			frappe.set_user("Administrator")
		self.TEST_CLIENT.set_cookie(key="sid", value=sid)
		return sid

	def command(self, method, payload, *, request_key=None):
		return self.post(
			self.method(PREFIX + method),
			{"payload": payload},
			headers={"Idempotency-Key": request_key or fixture_request(method)},
		)

	def success(self, response):
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		return response.get_json()["message"]

	def open_case(self):
		return self.success(
			self.command("request_case", {"submission": self.submission["name"], "expected_revision": 1})
		)

	def prepare(self, case, *, action="Approve", revision=1):
		return self.command(
			"prepare",
			{
				"case": case,
				"expected_revision": revision,
				"action": action,
				"reason": "已人工核对科室申报与资金来源",
			},
		)

	def confirm(self, preview, *, request_key=None, acknowledge=True):
		return self.command(
			"confirm",
			{"confirmation": preview["confirmation"], "proof": preview["proof"], "acknowledge": acknowledge},
			request_key=request_key,
		)

	def read_case(self, name):
		return self.get(self.method(PREFIX + "get_record"), {"name": name})

	def test_all_votes_persist_confirmation_and_native_workflow_without_financial_posting(self):
		before = {dt: frappe.db.count(dt) for dt in ("Budget", "GL Entry", "Stock Ledger Entry", "DocShare")}
		case = self.open_case()["name"]
		self.assertEqual(
			frappe.db.count("ToDo", {"reference_type": CASE, "reference_name": case, "status": "Open"}), 2
		)
		self.assertFalse(frappe.has_permission(CASE, "write", doc=frappe.get_doc(CASE, case), user=ACTORS[0]))
		self.login_actor(ACTORS[0])
		preview = self.success(self.prepare(case))
		self.assertEqual(frappe.db.get_value(CASE, case, "revision"), 1)
		self.assertEqual(self.confirm(preview, acknowledge=False).status_code, 400)
		request_key = fixture_request("confirm-retry")
		self.assertEqual(self.success(self.confirm(preview, request_key=request_key))["status"], "Pending")
		self.assertTrue(self.success(self.confirm(preview, request_key=request_key))["idempotency_replayed"])
		self.assertEqual(self.confirm(preview).status_code, 409)
		self.assertEqual(frappe.db.count(REVIEW, {"case": case, "event_kind": "Decision"}), 1)
		self.login_actor(ACTORS[1])
		second = self.success(self.prepare(case, revision=2))
		self.assertEqual(self.success(self.confirm(second))["status"], "Approved")
		self.assertEqual(frappe.db.get_value(CASE, case, "docstatus"), 1)
		item = self.success(self.read_case(case))
		self.assertEqual(item["total_amount"], "0.10")
		self.assertEqual(len(item["reviews"]), 2)
		self.assertNotIn("decision_snapshot", item)
		self.assertNotIn(preview["proof"], str(item))
		self.assertEqual(
			frappe.db.count("ToDo", {"reference_type": CASE, "reference_name": case, "status": "Open"}), 0
		)
		self.assertEqual(
			frappe.db.count("Workflow Action", {"reference_doctype": CASE, "reference_name": case}), 0
		)
		self.assertEqual(before, {dt: frappe.db.count(dt) for dt in before})

	def test_sequential_steps_reject_future_actor_and_then_advance(self):
		frappe.set_user("Administrator")
		self.matrix = self.configure(sequential=True)
		frappe.local.db.commit()
		case = self.open_case()["name"]
		self.login_actor(ACTORS[1])
		self.assertEqual(self.prepare(case).status_code, 403)
		self.login_actor(ACTORS[0])
		self.success(self.confirm(self.success(self.prepare(case))))
		self.assertEqual(frappe.db.get_value(CASE, case, "current_step"), 2)
		self.login_actor(ACTORS[1])
		self.assertEqual(
			self.success(self.confirm(self.success(self.prepare(case, revision=2))))["status"], "Approved"
		)

	def test_any_vote_finishes_and_old_preview_cannot_be_consumed(self):
		frappe.set_user("Administrator")
		self.matrix = self.configure(mode="Any")
		frappe.local.db.commit()
		case = self.open_case()["name"]
		self.login_actor(ACTORS[1])
		old = self.success(self.prepare(case))
		self.login_actor(ACTORS[0])
		self.assertEqual(self.success(self.confirm(self.success(self.prepare(case))))["status"], "Approved")
		self.login_actor(ACTORS[1])
		self.assertEqual(self.confirm(old).status_code, 409)

	def test_rejection_and_withdrawal_require_confirmation_and_create_new_rounds(self):
		case = self.open_case()["name"]
		self.login_actor(ACTORS[0])
		self.assertEqual(
			self.success(self.confirm(self.success(self.prepare(case, action="Reject"))))["status"],
			"Rejected",
		)
		self.login_actor(PLANNER)
		next_case = self.open_case()["name"]
		self.assertNotEqual(case, next_case)
		self.assertEqual(frappe.db.get_value(CASE, next_case, "round_number"), 2)
		preview = self.success(self.prepare(next_case, action="Withdraw"))
		self.assertEqual(self.success(self.confirm(preview))["status"], "Withdrawn")
		self.assertEqual(frappe.db.get_value(CASE, next_case, "docstatus"), 0)

	def test_confirmation_is_bound_to_actor_and_web_session(self):
		case = self.open_case()["name"]
		self.login_actor(ACTORS[0])
		preview = self.success(self.prepare(case))
		self.login_actor(ACTORS[1])
		self.assertEqual(self.confirm(preview).status_code, 403)
		self.login_actor(ACTORS[0])
		self.assertEqual(self.confirm(preview).status_code, 403)
		self.assertEqual(frappe.db.count(REVIEW, {"case": case, "event_kind": "Decision"}), 0)

	def test_expired_preview_and_changed_matrix_are_rejected_without_decision(self):
		case = self.open_case()["name"]
		self.login_actor(ACTORS[0])
		preview = self.success(self.prepare(case))
		# Only the test corrupts stored evidence to simulate a timestamp in the past.
		row = frappe.get_doc(REVIEW, preview["confirmation"])
		row.expires_at = now_datetime() - timedelta(seconds=1)
		frappe.db.set_value(
			REVIEW,
			row.name,
			{"expires_at": row.expires_at, "event_digest": fingerprint_json(evidence_values(row))},
		)
		frappe.local.db.commit()
		self.assertEqual(self.confirm(preview).status_code, 409)
		fresh = self.success(self.prepare(case))
		frappe.db.set_value("HRP Approval Matrix", self.matrix["name"], "enabled", 0)
		frappe.local.db.commit()
		self.assertNotEqual(self.confirm(fresh).status_code, 200)
		self.assertEqual(frappe.db.count(REVIEW, {"case": case, "event_kind": "Decision"}), 0)

	def test_current_scope_and_source_share_revoke_read_list_and_confirmation(self):
		case = self.open_case()["name"]
		self.login_actor(ACTORS[0])
		preview = self.success(self.prepare(case))
		frappe.set_user("Administrator")
		frappe.share.remove("HRP Hospital", HOSPITAL, ACTORS[0])
		frappe.local.db.commit()
		try:
			self.assertEqual(self.read_case(case).status_code, 403)
			self.assertEqual(self.confirm(preview).status_code, 403)
			listed = self.success(self.get(self.method(PREFIX + "list_records"), {"filters": {"name": case}}))
			self.assertEqual(listed["items"], [])
		finally:
			frappe.set_user("Administrator")
			share_test_records("HRP Hospital", (HOSPITAL,), (ACTORS[0],))
			frappe.local.db.commit()

	def test_native_save_workflow_and_todo_closure_cannot_approve(self):
		case = self.open_case()["name"]
		frappe.set_user("Administrator")
		for operation in (
			lambda: frappe.get_doc(CASE, case).save(ignore_permissions=True),
			lambda: apply_workflow(frappe.get_doc(CASE, case), WORKFLOW_ACTIONS["Approved"]),
			lambda: frappe.rename_doc(CASE, case, "invalid-budget-case", force=True),
		):
			with self.assertRaises((IoneApplicationError, frappe.PermissionError)):
				operation()
		frappe.db.rollback()
		frappe.set_user(ACTORS[0])
		todo = frappe.get_doc(
			"ToDo", {"reference_type": CASE, "reference_name": case, "allocated_to": ACTORS[0]}
		)
		todo.status = "Closed"
		todo.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(CASE, case, "revision"), 1)
		self.assertEqual(frappe.db.count(REVIEW, {"case": case, "event_kind": "Decision"}), 0)
		frappe.db.rollback()

	def test_workflow_definition_expressions_and_owned_identity_rename_are_denied(self):
		self.open_case()
		frappe.set_user("Administrator")
		workflow = require_workflow_integrity()
		workflow.transitions[0].condition = "doc.total_amount"
		with self.assertRaises(IoneApplicationError):
			workflow.save(ignore_permissions=True)
		frappe.db.rollback()
		self.assertEqual(require_workflow_integrity().name, WORKFLOW)
		with self.assertRaises(IoneApplicationError):
			frappe.rename_doc("User", ACTORS[0], "cod032-renamed@example.com", force=True)
		frappe.db.rollback()

	def test_self_approval_guest_and_unconfirmed_payload_fail_closed(self):
		case = self.open_case()["name"]
		self.assertEqual(self.prepare(case).status_code, 403)
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		for method in ("request_case", "prepare", "confirm"):
			self.assertEqual(self.command(method, []).status_code, 401)
		self.login_actor(ACTORS[0])
		self.assertEqual(self.command("confirm", {"case": case, "action": "Approve"}).status_code, 400)

	def test_missing_segregation_rule_does_not_create_confirmation(self):
		case = self.open_case()["name"]
		frappe.db.set_value("HRP Segregation Rule", self.rule["name"], "enabled", 0)
		frappe.local.db.commit()
		self.login_actor(ACTORS[0])
		self.assertNotEqual(self.prepare(case).status_code, 200)
		self.assertEqual(frappe.db.count(REVIEW, {"case": case}), 0)
