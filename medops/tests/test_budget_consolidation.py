from __future__ import annotations

import frappe
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.approval_matrix import build_approval_matrix_steps, build_approval_matrix_upsert
from medops.common.budget_approval import build_department_submission
from medops.common.budget_planning import (
	MASTER_TYPES,
	build_action,
	build_master,
	build_plan,
	build_version,
	master_identity,
)
from medops.common.budget_workflow import APPROVER_ROLE, CASE
from medops.common.error_catalog import IoneApplicationError
from medops.common.organization import (
	build_hierarchy_replace,
	build_hospital_upsert,
	build_organization_version_create,
	build_organization_version_publish,
)
from medops.common.organization_mapping import build_organization_mapping_upsert
from medops.common.segregation import build_segregation_rule_upsert
from medops.hrp_budget.services.approval_access import get_case
from medops.hrp_budget.services.planning import freeze_budget_version, save_budget_plan, save_budget_version
from medops.hrp_budget.services.submission import seal_department_submission
from medops.hrp_master_data.services.budget_dimensions import upsert_budget_master
from medops.hrp_organization.services.organization import (
	create_organization_version,
	publish_organization_version,
	replace_organization_hierarchy,
	upsert_hospital,
)
from medops.hrp_organization.services.organization_mapping import upsert_organization_mapping
from medops.hrp_workflow_authorization.services.approval_matrix import upsert_approval_matrix
from medops.hrp_workflow_authorization.services.segregation import upsert_segregation_rule
from medops.tests import test_budget_workflow as workflow_fixture
from medops.tests.scope_fixtures import grant_test_scope, share_test_records
from medops.tests.test_budget_submission import fixture_request

COMPANY = "COD-032C汇总测试法人"
HOSPITAL = "COD032C-HOSPITAL"
YEAR = "COD032C-FY-2026"
MANAGER = "cod032c-manager@example.com"
MAKERS = ("cod032c-maker-a@example.com", "cod032c-maker-b@example.com")
REVIEWER = "cod032c-reviewer@example.com"
PREFIX = "medops.api.v1.finance."


def fixture_master(kind, payload):
	command = build_master(kind, payload)
	name = master_identity(command)
	if frappe.db.exists(MASTER_TYPES[kind], name):
		return {"name": name}
	return upsert_budget_master(command, idempotency_key=fixture_request("c-" + kind))


def fixtures():
	frappe.set_user("Administrator")
	if not frappe.db.exists("Company", COMPANY):
		frappe.get_doc(
			dict(
				doctype="Company", company_name=COMPANY, abbr="C032C", country="China", default_currency="CNY"
			)
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Fiscal Year", YEAR):
		frappe.get_doc(
			dict(
				doctype="Fiscal Year",
				year=YEAR,
				year_start_date="2026-01-01",
				year_end_date="2026-12-31",
				companies=[dict(company=COMPANY)],
			)
		).insert(ignore_permissions=True)
	if not frappe.db.exists("HRP Hospital", HOSPITAL):
		upsert_hospital(
			build_hospital_upsert(code=HOSPITAL, company=COMPANY, display_name="汇总验收医院"),
			idempotency_key=fixture_request("c-hospital"),
		)
	org = frappe.db.get_value("HRP Organization Version", {"hospital": HOSPITAL, "docstatus": 1}, "name")
	if not org:
		created = create_organization_version(
			build_organization_version_create(
				hospital=HOSPITAL, effective_from="2026-01-01", version_label="汇总组织"
			),
			idempotency_key=fixture_request("c-org"),
		)
		nodes = [
			dict(
				code=HOSPITAL,
				display_name="测试医院",
				unit_type="HOSPITAL",
				is_group=True,
				enabled=True,
				valid_from="2026-01-01",
			)
		]
		nodes += [
			dict(
				code=code,
				display_name="科室" + code,
				unit_type="CLINICAL_DEPARTMENT",
				parent_code=HOSPITAL,
				is_group=False,
				enabled=True,
				valid_from="2026-01-01",
			)
			for code in ("A", "B")
		]
		tree = replace_organization_hierarchy(
			build_hierarchy_replace(
				organization_version=created["name"], expected_revision=created["revision"], nodes=nodes
			),
			idempotency_key=fixture_request("c-tree"),
		)
		org = publish_organization_version(
			build_organization_version_publish(
				organization_version=created["name"], expected_revision=tree["revision"]
			),
			idempotency_key=fixture_request("c-publish"),
		)["name"]
	units = {
		code: frappe.db.get_value(
			"HRP Organization Unit", {"organization_version": org, "code": code}, "name"
		)
		for code in (HOSPITAL, "A", "B")
	}
	parent = frappe.db.get_value("Cost Center", {"company": COMPANY, "is_group": 1}, "name")
	for code in ("A", "B"):
		center = frappe.db.get_value(
			"Cost Center", {"company": COMPANY, "cost_center_name": "COD032C " + code}, "name"
		)
		if not center:
			center = (
				frappe.get_doc(
					dict(
						doctype="Cost Center",
						cost_center_name="COD032C " + code,
						company=COMPANY,
						parent_cost_center=parent,
						is_group=0,
					)
				)
				.insert(ignore_permissions=True)
				.name
			)
		if not frappe.db.exists("HRP Organization Mapping", {"organization_unit": units[code]}):
			upsert_organization_mapping(
				build_organization_mapping_upsert(
					organization_version=org, organization_unit=units[code], cost_center=center
				),
				idempotency_key=fixture_request("c-map"),
			)
	for email, role in (
		(MANAGER, "HRP Budget Manager"),
		*((email, "HRP Budget User") for email in MAKERS),
		(REVIEWER, APPROVER_ROLE),
	):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				dict(
					doctype="User",
					email=email,
					first_name="Synthetic Consolidation",
					enabled=1,
					user_type="System User",
					send_welcome_email=0,
					roles=[dict(role=value) for value in ("HRP User", role, "Accounts User")],
				)
			).insert(ignore_permissions=True)
		grant_test_scope(
			code="COD032C-scope-" + email.split("@")[0], company=COMPANY, hospital=HOSPITAL, users=(email,)
		)
	users = (MANAGER, *MAKERS, REVIEWER)
	for doctype, names in (
		("HRP Hospital", (HOSPITAL,)),
		("HRP Organization Version", (org,)),
		("HRP Organization Unit", tuple(units.values())),
		(
			"HRP Organization Mapping",
			tuple(frappe.get_all("HRP Organization Mapping", filters={"hospital": HOSPITAL}, pluck="name")),
		),
	):
		share_test_records(doctype, tuple(str(name) for name in names), users)
	base = dict(company=COMPANY, hospital=HOSPITAL, valid_from="2026-01-01")
	dimension = fixture_master(
		"dimension",
		dict(**base, code="COD032C-SUBJECT", display_name="预算分类", purpose="Budget Subject"),
	)
	value = fixture_master(
		"value", dict(**base, code="EQUIPMENT", display_name="设备预算", dimension=dimension["name"])
	)
	fund = fixture_master(
		"fund", dict(**base, code="COD032C-OWN", display_name="自有资金", source_type="Own Revenue")
	)
	for code, unit, kind in (
		("COD032C-DEPARTMENT", None, "User"),
		("COD032C-CONSOLIDATED", units[HOSPITAL], "Role"),
	):
		if not frappe.db.exists("HRP Approval Matrix", code):
			step = dict(
				sequence_no=1,
				step_name="独立财务复核",
				threshold_amount="0.000000000",
				approver_type=kind,
				approval_mode="Any",
			)
			step.update({"approver_user": REVIEWER} if kind == "User" else {"approver_role": APPROVER_ROLE})
			upsert_approval_matrix(
				build_approval_matrix_upsert(
					code=code,
					display_name=code,
					target_doctype=CASE,
					company=COMPANY,
					hospital=HOSPITAL,
					organization_unit=unit,
					valid_from="2026-01-01",
					priority=100 if unit else 10,
					dimensions={"fiscal_year": YEAR, "currency": "CNY"},
					schema_version=2,
					amount_precision=9,
					steps=build_approval_matrix_steps([step], schema_version=2, amount_precision=9),
				),
				idempotency_key=fixture_request("c-matrix"),
			)
	if not frappe.db.exists("HRP Segregation Rule", "COD032C-SOD"):
		upsert_segregation_rule(
			build_segregation_rule_upsert(
				code="COD032C-SOD",
				display_name="原始来源职责分离",
				target_doctype=CASE,
				target_action="approve",
				company=COMPANY,
				hospital=HOSPITAL,
				organization_unit=None,
				enabled=True,
				valid_from="2026-01-01",
				actor_fields=("applicant", "source_applicant", "requested_by"),
				conflicting_roles=(),
			),
			idempotency_key=fixture_request("c-sod"),
		)
	frappe.local.db.commit()
	return org, units, value["name"], fund["name"]


class TestBudgetConsolidationAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.org, cls.units, cls.value, cls.fund = fixtures()
		cls.base_sources = None

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		if self.__class__.base_sources is None:
			self.__class__.base_sources = self.new_sources()
		self.sources = self.__class__.base_sources
		self.login_actor(MANAGER)

	def login_actor(self, user):
		return workflow_fixture.TestBudgetWorkflowAPI.login_actor(self, user)

	def success(self, response):
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		return response.get_json()["message"]

	def post_command(self, endpoint, payload, request_key=None):
		response = self.post(
			self.method(PREFIX + endpoint),
			{"payload": payload},
			headers={"Idempotency-Key": request_key or fixture_request("consolidation-http")},
		)
		frappe.db.rollback()
		return response

	def get_record(self, name):
		return self.get(self.method(PREFIX + "submission.get_record"), {"name": name})

	def proposal(self, sources=None, root=None):
		return dict(
			organization_unit=root or self.units[HOSPITAL],
			subject="综合预算汇总验收",
			sources=sources or self.sources,
		)

	def seal(self, sources=None, root=None, request_key=None):
		return self.post_command("consolidation.seal", self.proposal(sources, root), request_key)

	def base_aggregate(self):
		key = "COD032C-aggregate-" + "-".join(row["submission"][:16] for row in self.sources)
		return self.success(self.seal(request_key=key))["name"]

	def request_approval(self, name):
		return self.success(
			self.post_command("approval.request_case", dict(submission=name, expected_revision=1))
		)["name"]

	def decide(self, case, action="Approve", revision=1):
		preview = self.success(
			self.post_command(
				"approval.prepare",
				dict(case=case, expected_revision=revision, action=action, reason="合成汇总独立验收"),
			)
		)
		return self.post_command(
			"approval.confirm",
			dict(confirmation=preview["confirmation"], proof=preview["proof"], acknowledge=True),
		)

	def new_sources(self, amounts=None):
		frappe.set_user("Administrator")
		precision = frappe.db.get_default("currency_precision")
		frappe.db.set_default("currency_precision", "9")
		version = save_budget_version(
			build_version(
				dict(
					company=COMPANY,
					hospital=HOSPITAL,
					organization_version=self.org,
					fiscal_year=YEAR,
					subject="汇总原始编制",
				)
			),
			idempotency_key=fixture_request("c-version"),
		)
		account = frappe.db.get_value(
			"Account",
			{"company": COMPANY, "is_group": 0, "disabled": 0, "report_type": "Profit and Loss"},
			"name",
		)
		plans = []
		amounts = ("99999.999999999", "0.000000002") if amounts is None else amounts
		for code, user, amount in zip(("A", "B"), MAKERS, amounts, strict=True):
			frappe.set_user(user)
			plans.append(
				save_budget_plan(
					build_plan(
						dict(
							budget_version=version["name"],
							organization_unit=self.units[code],
							subject="原始科室" + code,
							lines=[
								dict(
									budget_subject=self.value,
									funding_source=self.fund,
									account=account,
									period_index=1,
									amount=amount,
								)
							],
						)
					),
					idempotency_key=fixture_request("c-plan"),
				)
			)
		frappe.set_user("Administrator")
		freeze_budget_version(
			build_action(dict(name=version["name"], expected_revision=1)),
			idempotency_key=fixture_request("c-freeze"),
		)
		leaves = []
		for plan, user in zip(plans, MAKERS, strict=True):
			frappe.set_user(user)
			leaves.append(
				seal_department_submission(
					build_department_submission(dict(source_plan=plan["name"], expected_revision=1)),
					idempotency_key=fixture_request("c-leaf"),
				)["name"]
			)
		frappe.set_user("Administrator")
		frappe.db.set_default("currency_precision", precision)
		frappe.local.db.commit()
		result = []
		for leaf, user in zip(leaves, MAKERS, strict=True):
			self.login_actor(user)
			case = self.request_approval(leaf)
			self.login_actor(REVIEWER)
			self.success(self.decide(case))
			result.append(
				dict(
					submission=leaf,
					expected_revision=1,
					approval_case=case,
					expected_approval_revision=2,
					snapshot_digest=frappe.db.get_value("HRP Budget Submission", leaf, "snapshot_digest"),
				)
			)
		self.login_actor(MANAGER)
		return result

	def test_exact_full_source_snapshot_replay_and_no_financial_posting(self):
		before = {dt: frappe.db.count(dt) for dt in ("Budget", "GL Entry", "Stock Ledger Entry", "DocShare")}
		name = self.base_aggregate()
		item = self.success(self.get_record(name))
		self.assertEqual(
			(item["schema_version"], item["submission_type"], item["total_amount"]),
			(2, "Consolidated", "100000.000000001"),
		)
		self.assertIsNone(item["cost_center"])
		self.assertEqual(len(item["sources"]), 2)
		self.assertEqual(len(item["lines"]), 2)
		self.assertEqual(
			{row["organization_unit"] for row in item["lines"]}, {self.units["A"], self.units["B"]}
		)
		self.assertEqual(len({row["cost_center"] for row in item["lines"]}), 2)
		self.assertNotIn("origin_version_digest", item["lines"][0])
		self.assertNotIn("sources_snapshot", item)
		self.assertEqual(self.seal().status_code, 409)
		self.assertEqual(before, {dt: frappe.db.count(dt) for dt in before})

	def test_large_consolidated_amount_can_complete_real_human_approval_without_rounding(self):
		sources = self.new_sources(("999999999999.000000001", "999999999999.000000001"))
		name = self.success(self.seal(sources=sources))["name"]
		case = self.request_approval(name)
		self.login_actor(REVIEWER)
		self.success(self.decide(case))
		self.login_actor(MANAGER)
		approved = self.success(self.get(self.method(PREFIX + "approval.get_record"), {"name": case}))
		self.assertEqual(approved["total_amount"], "1999999999998.000000002")
		self.assertEqual((approved["status"], approved["docstatus"]), ("Approved", 1))
		frappe.set_user("Administrator")
		self.assertEqual(get_case(case).total_amount, "1999999999998.000000002")

	def test_eligible_sources_are_approved_department_leaves_and_bounded(self):
		name = self.base_aggregate()
		response = self.get(
			self.method(PREFIX + "consolidation.eligible"), {"filters": {"hospital": HOSPITAL}, "limit": 100}
		)
		items = self.success(response)["items"]
		self.assertTrue(
			{row["submission"] for row in self.sources}.issubset({row["submission"] for row in items})
		)
		self.assertNotIn(name, {row["submission"] for row in items})
		self.assertEqual(
			self.get(
				self.method(PREFIX + "consolidation.eligible"), {"filters": {"hospital": ["like", "%"]}}
			).status_code,
			400,
		)

	def test_current_source_share_revocation_denies_read_list_title_and_replay(self):
		name = self.base_aggregate()
		frappe.set_user("Administrator")
		frappe.share.remove("HRP Organization Unit", self.units["B"], MANAGER)
		frappe.local.db.commit()
		try:
			self.assertEqual(self.get_record(name).status_code, 403)
			self.assertEqual(self.seal().status_code, 403)
			self.assertEqual(
				self.get(
					self.method("frappe.desk.search.get_link_title"),
					{"doctype": "HRP Budget Submission", "docname": name},
				).status_code,
				403,
			)
			listed = self.success(
				self.get(self.method(PREFIX + "submission.list_records"), {"filters": {"name": name}})
			)
			self.assertEqual(listed["items"], [])
		finally:
			frappe.set_user("Administrator")
			share_test_records("HRP Organization Unit", (self.units["B"],), (MANAGER,))
			frappe.local.db.commit()

	def test_original_preparers_and_requestors_cannot_approve_combined_sources(self):
		name = self.base_aggregate()
		frappe.set_user("Administrator")
		for user in (*MAKERS, MANAGER):
			frappe.get_doc("User", user).add_roles(APPROVER_ROLE)
		frappe.local.db.commit()
		try:
			case = self.request_approval(name)
			frappe.set_user("Administrator")
			decision = frappe.parse_json(get_case(case).decision_snapshot)
			self.assertEqual(decision["approvers"], [REVIEWER])
			for user in (*MAKERS, MANAGER):
				self.login_actor(user)
				response = self.post_command(
					"approval.prepare",
					dict(case=case, expected_revision=1, action="Approve", reason="自批必须阻断"),
				)
				self.assertEqual(response.status_code, 403)
			self.login_actor(REVIEWER)
			self.assertEqual(self.success(self.decide(case))["status"], "Approved")
		finally:
			frappe.set_user("Administrator")
			for user in (*MAKERS, MANAGER):
				frappe.get_doc("User", user).remove_roles(APPROVER_ROLE)
			frappe.local.db.commit()

	def test_different_proposals_cannot_approve_the_same_original_plan_twice(self):
		sources = self.new_sources()
		full = self.success(self.seal(sources))["name"]
		partial = self.success(self.seal(sources[:1]))["name"]
		first = self.request_approval(full)
		second = self.request_approval(partial)
		self.login_actor(REVIEWER)
		self.success(self.decide(first))
		self.assertEqual(self.decide(second).status_code, 409)
		self.assertEqual(frappe.db.get_value(CASE, second, "status"), "Pending")
		self.login_actor(MANAGER)
		self.success(self.decide(second, action="Withdraw"))

	def test_stale_revision_proof_nested_source_and_foreign_root_are_rejected(self):
		bad = [dict(row) for row in self.sources]
		bad[0]["expected_approval_revision"] = 3
		self.assertEqual(self.seal(bad).status_code, 409)
		self.assertEqual(self.seal(root=self.units["A"]).status_code, 409)
		name = self.base_aggregate()
		bad = [{**self.sources[0], "submission": name}]
		self.assertEqual(self.seal(bad).status_code, 409)

	def test_direct_native_mutation_and_private_source_proof_are_guarded(self):
		name = self.base_aggregate()
		frappe.set_user("Administrator")
		with self.assertRaises(IoneApplicationError):
			frappe.get_doc("HRP Budget Submission", name).save(ignore_permissions=True)
		frappe.db.rollback()
		native = self.get(
			self.method("frappe.desk.form.load.getdoc"), {"doctype": "HRP Budget Submission", "name": name}
		)
		self.assertEqual(native.status_code, 200, native.get_data(as_text=True))
		self.assertFalse(native.get_json()["docs"][0].get("sources_snapshot"))

	def test_guest_and_non_manager_cannot_create_or_inject_financial_facts(self):
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		self.assertEqual(self.seal().status_code, 401)
		self.login_actor(MAKERS[0])
		self.assertEqual(self.seal().status_code, 403)
		self.login_actor(MANAGER)
		self.assertEqual(
			self.post_command("consolidation.seal", {**self.proposal(), "total_amount": "1"}).status_code, 400
		)
