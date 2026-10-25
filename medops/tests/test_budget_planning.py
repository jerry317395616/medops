from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.budget_planning import (
	DIMENSION,
	FUND,
	LINE,
	PLAN,
	VALUE,
	VERSION,
	build_action,
	build_master,
	build_plan,
	build_version,
)
from medops.common.business_rules import rule_payload
from medops.common.domain_service import idempotency_record_name
from medops.common.error_catalog import IoneApplicationError
from medops.common.organization import (
	build_hierarchy_replace,
	build_hospital_upsert,
	build_organization_version_create,
	build_organization_version_publish,
)
from medops.common.organization_mapping import build_organization_mapping_upsert
from medops.hrp_budget.services.planning import (
	clone_budget_version,
	freeze_budget_version,
	list_budget,
	read_budget,
	save_budget_plan,
	save_budget_version,
	spread_budget_amount,
	withdraw_budget_plan,
)
from medops.hrp_budget.services.planning_access import get_plan, get_version
from medops.hrp_master_data.services.budget_dimensions import get_budget_master, upsert_budget_master
from medops.hrp_organization.services.organization import (
	create_organization_version,
	publish_organization_version,
	replace_organization_hierarchy,
	upsert_hospital,
)
from medops.hrp_organization.services.organization_mapping import upsert_organization_mapping
from medops.hrp_workflow_authorization.services.business_rules import UpsertBusinessRule
from medops.setup.budget_planning import ensure_budget_planning
from medops.tests.scope_fixtures import grant_test_scope, share_test_records

COMPANY = "COD-031预算测试法人"
HOSPITAL = "COD031-HOSPITAL"
YEAR = "COD031-FY-2026"
PLANNER = "cod031-planner@example.com"
MANAGER = "cod031-manager@example.com"


def key(value):
	return "COD031-" + value


def ensure_budget_fixtures():
	frappe.set_user("Administrator")
	ensure_budget_planning()
	if not frappe.db.exists("Company", COMPANY):
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": COMPANY,
				"abbr": "C031",
				"country": "China",
				"default_currency": "CNY",
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Fiscal Year", YEAR):
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": YEAR,
				"year_start_date": "2026-01-01",
				"year_end_date": "2026-12-31",
				"companies": [{"company": COMPANY}],
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("HRP Hospital", HOSPITAL):
		upsert_hospital(
			build_hospital_upsert(code=HOSPITAL, company=COMPANY, display_name="COD031预算测试医院"),
			idempotency_key=key("hospital"),
		)
	version = frappe.db.get_value("HRP Organization Version", {"hospital": HOSPITAL, "docstatus": 1}, "name")
	if not version:
		created = create_organization_version(
			build_organization_version_create(
				hospital=HOSPITAL, effective_from="2026-01-01", version_label="预算测试组织"
			),
			idempotency_key=key("org-create"),
		)
		replaced = replace_organization_hierarchy(
			build_hierarchy_replace(
				organization_version=created["name"],
				expected_revision=created["revision"],
				nodes=[
					{
						"code": HOSPITAL,
						"display_name": "测试医院",
						"unit_type": "HOSPITAL",
						"is_group": True,
						"enabled": True,
						"valid_from": "2026-01-01",
					},
					{
						"code": "OUT",
						"display_name": "门诊",
						"unit_type": "CLINICAL_DEPARTMENT",
						"parent_code": HOSPITAL,
						"is_group": False,
						"enabled": True,
						"valid_from": "2026-01-01",
					},
				],
			),
			idempotency_key=key("org-tree"),
		)
		version = publish_organization_version(
			build_organization_version_publish(
				organization_version=created["name"], expected_revision=replaced["revision"]
			),
			idempotency_key=key("org-publish"),
		)["name"]
	unit = frappe.db.get_value(
		"HRP Organization Unit", {"organization_version": version, "code": "OUT"}, "name"
	)
	center = frappe.db.get_value("Cost Center", {"company": COMPANY, "is_group": 0, "disabled": 0}, "name")
	if not frappe.db.exists("HRP Organization Mapping", {"organization_unit": unit}):
		upsert_organization_mapping(
			build_organization_mapping_upsert(
				organization_version=version, organization_unit=unit, cost_center=center
			),
			idempotency_key=key("mapping"),
		)
	for email, role in ((PLANNER, "HRP Budget User"), (MANAGER, "HRP Budget Manager")):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Synthetic Budget",
					"enabled": 1,
					"user_type": "System User",
					"send_welcome_email": 0,
					"roles": [{"role": "HRP User"}, {"role": role}, {"role": "Accounts User"}],
				}
			).insert(ignore_permissions=True)
		grant_test_scope(
			code=key("scope-" + email.split("@")[0]), company=COMPANY, hospital=HOSPITAL, users=(email,)
		)
	# Operational roles do not imply the steward's native organization access.
	# Supply explicit synthetic read shares, as a hospital deployment must do.
	for doctype, names in (
		("HRP Hospital", (HOSPITAL,)),
		("HRP Organization Version", (version,)),
		("HRP Organization Unit", (unit,)),
		(
			"HRP Organization Mapping",
			(frappe.db.get_value("HRP Organization Mapping", {"organization_unit": unit}, "name"),),
		),
	):
		share_test_records(doctype, tuple(str(name) for name in names), (PLANNER, MANAGER))
	frappe.local.db.commit()
	return str(version), str(unit)


def create_budget_masters():
	base = {"company": COMPANY, "hospital": HOSPITAL, "valid_from": "2026-01-01"}
	dimension = upsert_budget_master(
		build_master(
			"dimension",
			{**base, "code": "COD031-SUBJECT", "display_name": "预算分类", "purpose": "Budget Subject"},
		),
		idempotency_key=key("dimension"),
	)
	value = upsert_budget_master(
		build_master(
			"value", {**base, "code": "EQUIPMENT", "display_name": "设备预算", "dimension": dimension["name"]}
		),
		idempotency_key=key("value"),
	)
	fund = upsert_budget_master(
		build_master(
			"fund", {**base, "code": "OWN", "display_name": "自有资金", "source_type": "Own Revenue"}
		),
		idempotency_key=key("fund"),
	)
	return dimension, value, fund


def create_budget_version(org):
	return save_budget_version(
		build_version(
			{
				"company": COMPANY,
				"hospital": HOSPITAL,
				"organization_version": org,
				"fiscal_year": YEAR,
				"subject": "2026编制版本",
			}
		),
		idempotency_key=key("version"),
	)


def plan_command(version, unit, value, fund, *, amount="0.10", name=None, revision=0):
	account = frappe.db.get_value(
		"Account",
		{"company": COMPANY, "is_group": 0, "report_type": "Profit and Loss", "disabled": 0},
		"name",
	)
	return build_plan(
		{
			"name": name,
			"expected_revision": revision,
			"budget_version": version,
			"organization_unit": unit,
			"subject": "门诊年度计划",
			"lines": [
				{
					"budget_subject": value,
					"funding_source": fund,
					"account": account,
					"period_index": 1,
					"amount": amount,
				}
			],
		}
	)


class TestBudgetPlanningServices(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.org, cls.unit = ensure_budget_fixtures()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		# Pinned Frappe rolls back per class, not per method. Each scenario must
		# start from the committed fixture baseline, including idempotency rows.
		frappe.db.savepoint("cod031_case")
		self.dimension, self.value, self.fund = create_budget_masters()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="cod031_case")
		frappe.clear_cache()
		super().tearDown()

	def new_plan(self, amount="0.10"):
		version = create_budget_version(self.org)
		command = plan_command(
			version["name"], self.unit, self.value["name"], self.fund["name"], amount=amount
		)
		return version, command, save_budget_plan(command, idempotency_key=key("plan"))

	def test_models_repeat_migration_and_service_only_writes(self):
		for doctype in (DIMENSION, VALUE, FUND, VERSION, PLAN, LINE):
			meta = frappe.get_meta(doctype)
			self.assertFalse(meta.allow_rename)
			self.assertFalse(
				any(row.write or row.create or row.delete or row.submit for row in meta.permissions)
			)
		self.assertEqual(frappe.get_meta(PLAN).get_field("total_amount").fieldtype, "Data")
		self.assertEqual(ensure_budget_planning(), ensure_budget_planning())
		doc = frappe.get_doc(FUND, self.fund["name"])
		doc.display_name = "not a service command"
		with self.assertRaises(IoneApplicationError):
			doc.save(ignore_permissions=True)

	def test_master_writes_fence_the_native_company_row(self):
		# setUp has performed actual dimension/value/fund writes without commit.
		# A native rename on the second connection must wait for this fence.
		primary = frappe.local.db
		try:
			with self.secondary_connection():
				with self.assertRaises(frappe.QueryTimeoutError):
					frappe.db.sql("SELECT name FROM `tabCompany` WHERE name=%s FOR UPDATE NOWAIT", (COMPANY,))
		finally:
			frappe.local.db = primary

	def test_exact_money_round_trip_and_no_erp_posting(self):
		frappe.db.set_default("currency_precision", "9")
		before = {dt: frappe.db.count(dt) for dt in ("Budget", "GL Entry", "Stock Ledger Entry")}
		version, command, result = self.new_plan("999999999999.123456789")
		self.assertEqual(get_plan(result["name"]).reload().total_amount, "999999999999.123456789")
		self.assertEqual(read_budget("plan", result["name"])["lines"][0]["amount"], command.lines[0].amount)
		spread = spread_budget_amount(version["name"], "0.000000001")
		self.assertEqual([row["amount"] for row in spread].count("0.000000001"), 1)
		self.assertEqual(before, {dt: frappe.db.count(dt) for dt in before})

	def test_native_short_fiscal_year_clamps_month_edges_and_conserves_allocation(self):
		short = frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": "COD031-SHORT-2027",
				"is_short_year": 1,
				"year_start_date": "2027-02-10",
				"year_end_date": "2027-04-09",
				"companies": [{"company": COMPANY}],
			}
		).insert(ignore_permissions=True)
		version = save_budget_version(
			build_version(
				{
					"company": COMPANY,
					"hospital": HOSPITAL,
					"organization_version": self.org,
					"fiscal_year": short.name,
					"subject": "月中起止短年度",
				}
			),
			idempotency_key=key("short-year"),
		)
		periods = read_budget("version", version["name"])["periods"]
		self.assertEqual(
			[(x["period_start"], x["period_end"]) for x in periods],
			[
				("2027-02-10", "2027-02-28"),
				("2027-03-01", "2027-03-31"),
				("2027-04-01", "2027-04-09"),
			],
		)
		self.assertEqual(
			[x["amount"] for x in spread_budget_amount(version["name"], "0.02")], ["0.01", "0.01", "0.00"]
		)
		command = plan_command(version["name"], self.unit, self.value["name"], self.fund["name"])
		with self.assertRaises(IoneApplicationError):
			save_budget_plan(
				replace(command, lines=(replace(command.lines[0], period_index=4),)),
				idempotency_key=key("short-year-outside"),
			)
		self.assertEqual(frappe.db.count(PLAN, {"budget_version": version["name"]}), 0)

	def test_native_rename_sees_a_budget_reference_committed_after_old_snapshot(self):
		# Both connections use real domain writes. Native rename loads its source
		# before the guard, so a normal EXISTS query can retain an old snapshot.
		token = uuid4().hex[:12]
		version_key, plan_key = key("mvcc-version-" + token), key("mvcc-plan-" + token)
		primary = frappe.local.db
		isolation = str(frappe.db.sql("SELECT @@tx_isolation")[0][0])
		restore = {
			"REPEATABLE-READ": "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ",
			"READ-COMMITTED": "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
			"SERIALIZABLE": "SET SESSION TRANSACTION ISOLATION LEVEL SERIALIZABLE",
			"READ-UNCOMMITTED": "SET SESSION TRANSACTION ISOLATION LEVEL READ UNCOMMITTED",
		}[isolation]
		account, version, saved = None, None, None
		try:
			base = frappe.get_doc(
				"Account",
				plan_command("unused", self.unit, self.value["name"], self.fund["name"]).lines[0].account,
			)
			account = frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": "COD031-MVCC-" + token,
					"company": COMPANY,
					"parent_account": base.parent_account,
					"is_group": 0,
					"account_currency": base.account_currency,
					"root_type": base.root_type,
					"report_type": "Profit and Loss",
				}
			).insert(ignore_permissions=True)
			version = save_budget_version(
				build_version(
					{
						"company": COMPANY,
						"hospital": HOSPITAL,
						"organization_version": self.org,
						"fiscal_year": YEAR,
						"subject": "Synthetic old-snapshot reference fence",
					}
				),
				idempotency_key=version_key,
			)
			frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
			frappe.local.db.commit()
			self.assertEqual(frappe.db.count(LINE, {"account": account.name}), 0)
			command = plan_command(version["name"], self.unit, self.value["name"], self.fund["name"])
			with self.secondary_connection():
				saved = save_budget_plan(
					replace(command, lines=(replace(command.lines[0], account=account.name),)),
					idempotency_key=plan_key,
				)
				frappe.local.db.commit()
			frappe.local.db = primary
			self.assertEqual(
				frappe.db.count(LINE, {"account": account.name}),
				0,
				"Primary must still have the old snapshot",
			)
			with self.assertRaises(IoneApplicationError) as guarded:
				frappe.rename_doc("Account", account.name, "COD031-Renamed-" + token + " - C031", force=True)
			self.assertIn(guarded.exception.definition.key, {"CONFLICT", "OPERATION_NOT_ALLOWED"})
			primary.rollback()
			with self.assertRaises(IoneApplicationError) as current:
				frappe.rename_doc("Account", account.name, "COD031-Renamed-" + token + " - C031", force=True)
			self.assertEqual(current.exception.definition.key, "OPERATION_NOT_ALLOWED")
		finally:
			frappe.local.db = primary
			primary.rollback()
			frappe.set_user("Administrator")
			if saved:
				frappe.db.delete(LINE, {"parenttype": PLAN, "parent": saved["name"]})
				frappe.db.delete(PLAN, {"name": saved["name"]})
			if version:
				frappe.db.delete(VERSION, {"name": version["name"]})
			for service, request_key in (
				("hrp_budget.version.save", version_key),
				("hrp_budget.plan.save", plan_key),
			):
				frappe.db.delete(
					"HRP Service Idempotency", {"name": idempotency_record_name(service, request_key)}
				)
			if account and frappe.db.exists("Account", account.name):
				frappe.delete_doc("Account", account.name, ignore_permissions=True)
			frappe.db.sql(restore)
			primary.commit()
			primary.savepoint("cod031_case")

	def test_freeze_clone_and_old_financial_inputs_are_immutable(self):
		version, command, result = self.new_plan("0.30")
		previous_number = get_version(version["name"]).version_number
		frozen = freeze_budget_version(
			build_action({"name": version["name"], "expected_revision": 1}), idempotency_key=key("freeze")
		)
		self.assertEqual(frozen["docstatus"], 1)
		self.assertEqual(get_plan(result["name"]).docstatus, 0)
		with self.assertRaises(IoneApplicationError):
			save_budget_plan(
				replace(command, name=result["name"], expected_revision=1), idempotency_key=key("edit-frozen")
			)
		cloned = clone_budget_version(
			build_action({"name": version["name"], "expected_revision": 2}), idempotency_key=key("clone")
		)
		self.assertEqual(get_version(cloned["name"]).version_number, previous_number + 1)
		self.assertEqual(get_plan(result["name"]).total_amount, "0.30")
		new_plan = frappe.db.get_value(PLAN, {"budget_version": cloned["name"]}, "name")
		self.assertEqual(get_plan(new_plan).total_amount, "0.30")

	def test_withdraw_revise_and_replay_do_not_approve_or_duplicate(self):
		version, command, result = self.new_plan()
		self.assertTrue(save_budget_plan(command, idempotency_key=key("plan"))["idempotency_replayed"])
		withdraw_budget_plan(
			build_action({"name": result["name"], "expected_revision": 1}), idempotency_key=key("withdraw")
		)
		self.assertEqual(get_plan(result["name"]).status, "Withdrawn")
		with self.assertRaises(IoneApplicationError):
			freeze_budget_version(
				build_action({"name": version["name"], "expected_revision": 1}),
				idempotency_key=key("empty-freeze"),
			)
		save_budget_plan(
			replace(command, name=result["name"], expected_revision=2), idempotency_key=key("revise")
		)
		self.assertEqual(get_plan(result["name"]).status, "Draft")
		self.assertEqual(get_plan(result["name"]).docstatus, 0)
		self.assertEqual(frappe.db.count(PLAN, {"budget_version": version["name"]}), 1)

	def test_real_numeric_rule_denial_rolls_back_plan_lines_and_idempotency(self):
		version = create_budget_version(self.org)
		payload = rule_payload(
			{
				"code": "COD031-AMOUNT-RULE",
				"display_name": "合计不得高于九分",
				"target_doctype": PLAN,
				"target_action": "validate",
				"company": COMPANY,
				"hospital": HOSPITAL,
				"organization_unit": self.unit,
				"valid_from": "2026-01-01",
				"severity": "Block",
				"assertion_json": {"field": "total_amount", "op": "lte", "value": "0.09"},
			}
		)
		UpsertBusinessRule().execute({"policy": payload, "expected_revision": 0}, idempotency_key=key("rule"))
		before = {dt: frappe.db.count(dt) for dt in (PLAN, LINE, "HRP Service Idempotency")}
		with self.assertRaises(IoneApplicationError):
			save_budget_plan(
				plan_command(version["name"], self.unit, self.value["name"], self.fund["name"]),
				idempotency_key=key("rule-block"),
			)
		self.assertEqual(before, {dt: frappe.db.count(dt) for dt in before})

	def test_direct_plan_mutation_and_native_reference_rename_are_blocked(self):
		_, _, result = self.new_plan()
		doc = get_plan(result["name"])
		doc.subject = "bypass"
		with self.assertRaises(IoneApplicationError):
			doc.save(ignore_permissions=True)
		account = doc.lines[0].account
		with self.assertRaises(IoneApplicationError):
			frappe.rename_doc("Account", account, "COD031 Renamed - C031")
		self.assertTrue(frappe.db.exists("Account", account))

	def test_live_scope_revocation_and_generic_lists_cannot_leak(self):
		version, _, result = self.new_plan()
		frappe.set_user(PLANNER)
		self.assertEqual(read_budget("plan", result["name"])["total_amount"], "0.10")
		self.assertEqual(frappe.get_list(PLAN, pluck="name"), [])
		self.assertEqual(len(list_budget("plan", {"budget_version": version["name"]})["items"]), 1)
		frappe.set_user("Administrator")
		frappe.db.set_value("HRP Access Scope", key("scope-" + PLANNER.split("@")[0]), "enabled", 0)
		frappe.set_user(PLANNER)
		with self.assertRaises(IoneApplicationError):
			read_budget("plan", result["name"])
		self.assertEqual(list_budget("plan", {"hospital": HOSPITAL})["items"], [])

	def test_funding_effectivity_fails_closed_without_a_partial_plan(self):
		version = create_budget_version(self.org)
		fund = get_budget_master(FUND, self.fund["name"])
		upsert_budget_master(
			build_master(
				"fund",
				{
					"company": COMPANY,
					"hospital": HOSPITAL,
					"code": "OWN",
					"display_name": fund.display_name,
					"source_type": fund.source_type,
					"valid_from": "2026-02-01",
					"expected_revision": 1,
				},
			),
			idempotency_key=key("fund-later"),
		)
		with self.assertRaises(IoneApplicationError):
			save_budget_plan(
				plan_command(version["name"], self.unit, self.value["name"], self.fund["name"]),
				idempotency_key=key("fund-invalid"),
			)
		self.assertEqual(frappe.db.count(PLAN, {"budget_version": version["name"]}), 0)

	def test_successful_retry_survives_source_disablement_but_fresh_write_does_not(self):
		_, command, result = self.new_plan()
		fund = get_budget_master(FUND, self.fund["name"])
		upsert_budget_master(
			build_master(
				"fund",
				{
					"company": COMPANY,
					"hospital": HOSPITAL,
					"code": "OWN",
					"display_name": fund.display_name,
					"source_type": fund.source_type,
					"valid_from": str(fund.valid_from),
					"enabled": False,
					"expected_revision": 1,
				},
			),
			idempotency_key=key("disable-fund"),
		)
		replay = save_budget_plan(command, idempotency_key=key("plan"))
		self.assertTrue(replay["idempotency_replayed"])
		self.assertEqual(replay["name"], result["name"])
		self.assertNotIn("total_amount", replay)
		with self.assertRaises(IoneApplicationError):
			save_budget_plan(
				replace(command, name=result["name"], expected_revision=1),
				idempotency_key=key("new-after-disabled"),
			)
		self.assertEqual(get_plan(result["name"]).revision, 1)

	def test_native_source_share_revocation_does_not_grant_budget_read_via_scope(self):
		_, _, result = self.new_plan()
		frappe.set_user(PLANNER)
		self.assertEqual(read_budget("plan", result["name"])["total_amount"], "0.10")
		frappe.set_user("Administrator")
		frappe.share.remove("HRP Hospital", HOSPITAL, PLANNER)
		frappe.set_user(PLANNER)
		with self.assertRaises(IoneApplicationError):
			read_budget("plan", result["name"])
		self.assertEqual(list_budget("plan", {"hospital": HOSPITAL})["items"], [])

	def test_restricted_native_account_field_hides_plan_and_export(self):
		_, _, result = self.new_plan()
		field = frappe.db.get_value("DocField", {"parent": "Account", "fieldname": "disabled"}, "name")
		frappe.db.set_value("DocField", field, "permlevel", 1)
		frappe.clear_cache(doctype="Account")
		try:
			frappe.set_user(PLANNER)
			with self.assertRaises(IoneApplicationError):
				read_budget("plan", result["name"])
			with self.assertRaises(IoneApplicationError):
				get_plan(result["name"], export=True)
			self.assertEqual(list_budget("plan", {"hospital": HOSPITAL})["items"], [])
		finally:
			frappe.set_user("Administrator")
			frappe.db.set_value("DocField", field, "permlevel", 0)
			frappe.clear_cache(doctype="Account")


METHOD_PREFIX = "medops.api.v1.finance.planning."


class TestBudgetPlanningAPI(FrappeAPITestCase):
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

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("HRP Access Scope", key("scope-" + PLANNER.split("@")[0]), "enabled", 1)
		frappe.local.db.commit()
		super().tearDown()

	def write(self, method, payload, *, label=None):
		return self.post(
			self.method(METHOD_PREFIX + method),
			{"payload": payload},
			headers={"Idempotency-Key": key(self._testMethodName + "-" + (label or method))},
		)

	def create_version(self):
		payload = {
			"company": COMPANY,
			"hospital": HOSPITAL,
			"organization_version": self.org,
			"fiscal_year": YEAR,
			"subject": self._testMethodName,
		}
		response = self.write("save_version", payload)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		return response.get_json()["message"], payload

	def create_plan(self, version):
		command = plan_command(version["name"], self.unit, self.value["name"], self.fund["name"])
		payload = {
			**command.as_request_payload(),
			"lines": [row.as_request_payload() for row in command.lines],
		}
		payload.pop("lines_digest")
		payload.pop("line_count")
		response = self.write("save_plan", payload)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		return response.get_json()["message"], payload

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

	def test_http_full_lifecycle_and_acknowledgements(self):
		version, payload = self.create_version()
		replayed = self.write("save_version", payload)
		self.assertTrue(replayed.get_json()["message"]["idempotency_replayed"])
		plan, payload = self.create_plan(version)
		replayed = self.write("save_plan", payload)
		self.assertTrue(replayed.get_json()["message"]["idempotency_replayed"])
		self.assertNotIn("total_amount", replayed.get_json()["message"])
		spread = self.get(
			self.method(METHOD_PREFIX + "spread"), {"version": version["name"], "amount": "0.01"}
		)
		self.assertEqual(spread.status_code, 200)
		self.assertEqual(len(spread.get_json()["message"]["items"]), 12)
		read = self.get(self.method(METHOD_PREFIX + "get_record"), {"kind": "plan", "name": plan["name"]})
		self.assertEqual(read.get_json()["message"]["total_amount"], "0.10")
		frozen = self.write("freeze_version", {"name": version["name"], "expected_revision": 1})
		self.assertEqual(frozen.status_code, 200, frozen.get_data(as_text=True))
		cloned = self.write("clone_version", {"name": version["name"], "expected_revision": 2})
		self.assertEqual(cloned.status_code, 200, cloned.get_data(as_text=True))
		# HTTP writes use their own connection; read over HTTP again instead of
		# consulting this test thread's older repeatable-read snapshot.
		read = self.get(self.method(METHOD_PREFIX + "get_record"), {"kind": "plan", "name": plan["name"]})
		self.assertEqual(read.status_code, 200)
		self.assertEqual(read.get_json()["message"]["docstatus"], 0)

	def test_http_guest_rejected_before_malformed_input(self):
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		for method in (
			"save_version",
			"save_plan",
			"freeze_version",
			"clone_version",
			"withdraw_plan",
			"upsert_master",
		):
			self.assertEqual(self.post(self.method(METHOD_PREFIX + method), {"payload": []}).status_code, 401)
		for method in ("get_record", "list_records", "spread", "search", "export_plan"):
			self.assertEqual(
				self.get(self.method(METHOD_PREFIX + method), {"kind": "invalid"}).status_code, 401
			)

	def test_http_unknown_fields_and_structured_filter_operators_are_rejected(self):
		version, payload = self.create_version()
		for extra in ({"workflow_state": "Approved"}, {"currency_precision": 9}, {"unknown": []}):
			response = self.write("save_version", {**payload, **extra}, label="bad")
			self.assertEqual(response.status_code, 400)
		for params in (
			{"kind": "plan", "filters": {"hospital": ["!=", "hidden"]}},
			{"kind": "plan", "limit": 101},
			{"kind": "plan", "filters": {"snapshot_digest": "x"}},
		):
			response = self.get(self.method(METHOD_PREFIX + "list_records"), params)
			self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
		response = self.get(
			self.method(METHOD_PREFIX + "search"), {"doctype": PLAN, "searchfield": {"sql": "x"}}
		)
		self.assertEqual(response.status_code, 400)
		response = self.post(self.method(METHOD_PREFIX + "save_version"), {"payload": payload})
		self.assertEqual(response.status_code, 400)
		line = plan_command(version["name"], self.unit, self.value["name"], self.fund["name"]).lines[0]
		for amount in (0.1, 999999999999.0):
			response = self.write(
				"save_plan",
				{
					"budget_version": version["name"],
					"organization_unit": self.unit,
					"subject": "Reject decoded binary money",
					"lines": [{**line.as_request_payload(), "amount": amount}],
				},
				label="float-money",
			)
			self.assertEqual(response.status_code, 400, response.get_data(as_text=True))

	def test_http_scope_revocation_covers_read_list_search_export_and_replay(self):
		version, _ = self.create_version()
		plan, payload = self.create_plan(version)
		self.login_planner()
		read = self.get(self.method(METHOD_PREFIX + "get_record"), {"kind": "plan", "name": plan["name"]})
		self.assertEqual(read.status_code, 200, read.get_data(as_text=True))
		frappe.db.set_value("HRP Access Scope", key("scope-" + PLANNER.split("@")[0]), "enabled", 0)
		frappe.local.db.commit()
		for method in ("get_record", "export_plan"):
			params: dict[str, Any] = {"name": plan["name"]}
			if method == "get_record":
				params["kind"] = "plan"
			self.assertEqual(self.get(self.method(METHOD_PREFIX + method), params).status_code, 403)
		listed = self.get(
			self.method(METHOD_PREFIX + "list_records"),
			{"kind": "plan", "filters": {"budget_version": version["name"]}},
		)
		self.assertEqual(listed.status_code, 200)
		self.assertEqual(listed.get_json()["message"]["items"], [])
		searched = self.get(self.method(METHOD_PREFIX + "search"), {"doctype": PLAN, "txt": plan["name"]})
		self.assertEqual(searched.status_code, 200)
		self.assertEqual(searched.get_json()["message"], [])
		validated = self.get(
			self.method("frappe.client.validate_link_and_fetch"),
			{
				"doctype": PLAN,
				"docname": plan["name"],
				"query": METHOD_PREFIX + "search",
				"reference_doctype": "",
				"filters": {"hospital": HOSPITAL},
				"fields_to_fetch": [],
			},
		)
		self.assertEqual(validated.status_code, 200, validated.get_data(as_text=True))
		self.assertEqual(validated.get_json()["message"], {})
		self.assertEqual(self.write("save_plan", payload).status_code, 403)
		self.assertEqual(
			self.write("freeze_version", {"name": version["name"], "expected_revision": 1}).status_code, 403
		)

	def test_http_master_maintenance_custom_link_search_and_export(self):
		version, _ = self.create_version()
		plan, _ = self.create_plan(version)
		exported = self.get(self.method(METHOD_PREFIX + "export_plan"), {"name": plan["name"]})
		self.assertEqual(exported.status_code, 200, exported.get_data(as_text=True))
		self.assertEqual(exported.get_json()["message"]["rows"][0][7], "0.10")
		response = self.post(
			self.method(METHOD_PREFIX + "upsert_master"),
			{
				"kind": "fund",
				"payload": {
					"company": COMPANY,
					"hospital": HOSPITAL,
					"code": "HTTP",
					"display_name": "HTTP资金",
					"source_type": "Other",
					"valid_from": "2026-01-01",
				},
			},
			headers={"Idempotency-Key": key("http-fund")},
		)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		self.login_planner()
		search = self.get(
			self.method(METHOD_PREFIX + "search"),
			{"doctype": FUND, "txt": "HTTP", "filters": {"hospital": HOSPITAL}},
		)
		self.assertEqual(search.status_code, 200, search.get_data(as_text=True))
		self.assertEqual(search.get_json()["message"][0][1], "HTTP资金")
		for http in (self.get, self.post):
			response = http(
				self.method("frappe.desk.search.search_link"),
				{
					"doctype": FUND,
					"txt": "HTTP",
					"query": METHOD_PREFIX + "search",
					"reference_doctype": "",
					"filters": {"hospital": HOSPITAL},
				},
			)
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
			self.assertEqual(response.get_json()["message"][0]["value"], search.get_json()["message"][0][0])
			response = http(
				self.method("frappe.client.validate_link_and_fetch"),
				{
					"doctype": FUND,
					"docname": search.get_json()["message"][0][0],
					"query": METHOD_PREFIX + "search",
					"reference_doctype": "",
					"link_fieldname": "funding_source",
					"filters": {"hospital": HOSPITAL},
					"fields_to_fetch": [],
				},
			)
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
			self.assertEqual(response.get_json()["message"]["name"], search.get_json()["message"][0][0])
		self.assertEqual(self.post(self.method(METHOD_PREFIX + "upsert_master"), {}).status_code, 403)

	def test_http_friendly_titles_require_current_budget_source_and_scope_read(self):
		version, _ = self.create_version()
		plan, _ = self.create_plan(version)
		self.login_planner()
		for dt, name, title in (
			(VERSION, version["name"], self._testMethodName),
			(PLAN, plan["name"], "门诊年度计划"),
			(FUND, self.fund["name"], "自有资金"),
		):
			self.assertTrue(frappe.get_meta(dt).show_title_field_in_link)
			response = self.get(
				self.method("frappe.desk.search.get_link_title"), {"doctype": dt, "docname": name}
			)
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
			self.assertEqual(response.get_json()["message"], title)
		frappe.db.set_value("HRP Access Scope", key("scope-" + PLANNER.split("@")[0]), "enabled", 0)
		frappe.local.db.commit()
		for dt, name in ((VERSION, version["name"]), (PLAN, plan["name"]), (FUND, self.fund["name"])):
			response = self.get(
				self.method("frappe.desk.search.get_link_title"), {"doctype": dt, "docname": name}
			)
			self.assertEqual(response.status_code, 403, response.get_data(as_text=True))
			self.assertNotIn("门诊年度计划", response.get_data(as_text=True))
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		response = self.get(
			self.method("frappe.desk.search.get_link_title"), {"doctype": FUND, "docname": self.fund["name"]}
		)
		self.assertIn(response.status_code, (401, 403))
