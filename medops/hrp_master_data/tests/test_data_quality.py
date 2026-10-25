from __future__ import annotations

import json
from typing import cast
from unittest.mock import patch

import frappe
from frappe.boot import build_default_workspace_map, get_sidebar_items
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.data_quality import (
	build_data_quality_evaluate,
	build_data_quality_rule_upsert,
)
from medops.common.domain_service import idempotency_record_name
from medops.common.error_catalog import IoneApplicationError
from medops.common.master_data import build_master_data_domain_upsert
from medops.common.organization import build_hospital_upsert
from medops.hrp_master_data.doctype.hrp_data_quality_issue.hrp_data_quality_issue import (
	HRPDataQualityIssue,
)
from medops.hrp_master_data.permissions import can_read_data_quality, data_quality_query
from medops.hrp_master_data.services.data_quality import (
	DATA_QUALITY_BATCH_SIZE,
	EvaluateDataQualityService,
	UpsertDataQualityRuleService,
	evaluate_data_quality,
	get_data_quality_issue,
	run_data_quality_rule_batch,
	run_data_quality_rules,
	upsert_data_quality_rule,
)
from medops.hrp_master_data.services.master_data import (
	UpsertMasterDataDomainService,
	upsert_master_data_domain,
)
from medops.hrp_organization.services.organization import (
	UpsertHospitalService,
	upsert_hospital,
)
from medops.setup.master_data import ensure_master_data_governance

UPSERT_RULE_METHOD = "medops.api.v1.master_data.upsert_data_quality_rule"
EVALUATE_METHOD = "medops.api.v1.master_data.evaluate_data_quality"
GET_ISSUE_METHOD = "medops.api.v1.master_data.get_data_quality_issue"

PREFERRED_TEST_DOMAIN = "COD020-ITEM"
TEST_COMPANY = "COD-022测试医疗法人"
TEST_COMPANY_ABBR = "C022"
TEST_HOSPITAL = "COD022-HOSPITAL"
TEST_ITEM = "COD022-ITEM-001"
TEST_ITEM_GROUP = "COD022 Items"
TEST_ITEM_GROUP_ROOT = "COD022 All Item Groups"
TEST_STOCK_UOM = "COD022-UOM"
TEST_STEWARD = "cod022-steward@example.com"
TEST_PLAIN_USER = "cod022-plain@example.com"
RULE_SERVICE_NAMES = (
	UpsertDataQualityRuleService.definition.name,
	EvaluateDataQualityService.definition.name,
	UpsertMasterDataDomainService.definition.name,
	UpsertHospitalService.definition.name,
)


def data_quality_domain() -> str:
	return cast(
		str,
		frappe.db.get_value(
			"HRP Master Data Domain",
			{"target_doctype": "Item"},
			"name",
		)
		or PREFERRED_TEST_DOMAIN,
	)


def ensure_data_quality_fixtures() -> None:
	# Company creates a transit warehouse; do not depend on another test module's fixtures.
	if not frappe.db.exists("Warehouse Type", "Transit"):
		frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(ignore_permissions=True)
	if not frappe.db.exists("Company", TEST_COMPANY):
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": TEST_COMPANY,
				"abbr": TEST_COMPANY_ABBR,
				"country": "China",
				"default_currency": "CNY",
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("UOM", TEST_STOCK_UOM):
		frappe.get_doc(
			{
				"doctype": "UOM",
				"uom_name": TEST_STOCK_UOM,
				"must_be_whole_number": 0,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Item Group", TEST_ITEM_GROUP):
		root_item_group = frappe.db.get_value(
			"Item Group",
			{"parent_item_group": ("is", "not set")},
			"name",
		)
		if not root_item_group:
			root_item_group = (
				frappe.get_doc(
					{
						"doctype": "Item Group",
						"item_group_name": TEST_ITEM_GROUP_ROOT,
						"is_group": 1,
					}
				)
				.insert(ignore_permissions=True)
				.name
			)
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": TEST_ITEM_GROUP,
				"parent_item_group": root_item_group,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Item", TEST_ITEM):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": TEST_ITEM,
				"item_name": TEST_ITEM,
				"item_group": TEST_ITEM_GROUP,
				"stock_uom": TEST_STOCK_UOM,
				"is_stock_item": 0,
				"disabled": 0,
			}
		).insert(ignore_permissions=True)
	for email, role in (
		(TEST_STEWARD, "HRP Data Steward"),
		(TEST_PLAIN_USER, None),
	):
		if frappe.db.exists("User", email):
			user = frappe.get_doc("User", email)
		else:
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "COD-022",
					"last_name": "Steward" if role else "Plain",
					"enabled": 1,
					"send_welcome_email": 0,
				}
			)
			user.insert(ignore_permissions=True)
		if role and role not in frappe.get_roles(email):
			user.add_roles(role)
	frappe.local.db.commit()


def reset_data_quality_state() -> None:
	frappe.set_user("Administrator")
	frappe.db.delete("HRP Data Quality Issue", {"target_doctype": "Item"})
	frappe.db.delete("HRP Data Quality Rule", {"target_doctype": "Item"})
	frappe.db.delete(
		"HRP Service Idempotency",
		{"service_name": ("in", RULE_SERVICE_NAMES)},
	)
	frappe.db.set_value("Item", TEST_ITEM, "item_name", TEST_ITEM, update_modified=False)
	frappe.db.set_value("Item", TEST_ITEM, "disabled", 0, update_modified=False)


def create_data_quality_context(*, suffix: str) -> None:
	if not frappe.db.exists("HRP Hospital", TEST_HOSPITAL):
		upsert_hospital(
			build_hospital_upsert(
				code=TEST_HOSPITAL,
				company=TEST_COMPANY,
				display_name="COD-022测试医院",
				expected_revision=0,
			),
			idempotency_key=f"COD-022-hospital-{suffix}",
		)
	else:
		hospital = frappe.get_doc("HRP Hospital", TEST_HOSPITAL)
		if hospital.company != TEST_COMPANY or not bool(hospital.enabled):
			raise AssertionError("COD-022 test hospital must be operational")
	if not frappe.db.exists("HRP Master Data Domain", {"target_doctype": "Item"}):
		upsert_master_data_domain(
			build_master_data_domain_upsert(
				code=PREFERRED_TEST_DOMAIN,
				display_name="COD-022物料主数据",
				target_doctype="Item",
				expected_revision=0,
			),
			idempotency_key=f"COD-022-domain-{suffix}",
		)
	else:
		domain = frappe.get_doc("HRP Master Data Domain", data_quality_domain())
		if domain.target_doctype != "Item" or not bool(domain.enabled):
			raise AssertionError("Item master-data domain must be operational")


def rule_command(
	*,
	code: str = "DQ-ITEM-NAME",
	display_name: str = "物料名称长度",
	target_field: str = "item_name",
	rule_type: str = "Maximum Length",
	parameters: object = None,
	rule_name: str | None = None,
	expected_revision: int = 0,
	severity: str = "Major",
	enabled: bool = True,
	remarks: str | None = "COD-022受控质量规则",
):
	return build_data_quality_rule_upsert(
		rule_name=rule_name,
		code=code,
		display_name=display_name,
		master_data_domain=data_quality_domain(),
		company=TEST_COMPANY,
		hospital=TEST_HOSPITAL,
		target_field=target_field,
		rule_type=rule_type,
		parameters={"maximum": 8} if parameters is None else parameters,
		severity=severity,
		enabled=enabled,
		valid_from="2026-08-01",
		expected_revision=expected_revision,
		remarks=remarks,
	)


class TestDataQuality(IntegrationTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_data_quality_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_data_quality_state()

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	def _create_rule(self, *, suffix: str = "create") -> dict[str, object]:
		create_data_quality_context(suffix=suffix)
		return upsert_data_quality_rule(
			rule_command(),
			idempotency_key=f"COD-022-rule-{suffix}",
		)

	def test_metadata_migration_workspace_and_service_only_write(self) -> None:
		create_data_quality_context(suffix="metadata")
		rule_meta = frappe.get_meta("HRP Data Quality Rule")
		issue_meta = frappe.get_meta("HRP Data Quality Issue")
		self.assertEqual(rule_meta.module, "HRP Master Data")
		self.assertEqual(rule_meta.get_field("target_field").label, "校验字段")
		self.assertEqual(issue_meta.get_field("issue_status").label, "问题状态")
		self.assertEqual(len(rule_meta.fields), 17)
		self.assertEqual(len(issue_meta.fields), 20)
		for meta in (rule_meta, issue_meta):
			self.assertFalse(meta.is_submittable)
			self.assertFalse(any(permission.write for permission in meta.permissions))
			self.assertSetEqual(
				{permission.role for permission in meta.permissions},
				{"System Manager", "HRP System Manager", "HRP Data Steward"},
			)
		self.assertEqual(ensure_master_data_governance()["schema_version"], 1)
		self.assertEqual(ensure_master_data_governance()["schema_version"], 1)
		for doctype, expected in (
			("HRP Data Quality Rule", {"idx_hrp_data_quality_rule_schedule"}),
			(
				"HRP Data Quality Issue",
				{"uniq_hrp_data_quality_issue_key", "idx_hrp_data_quality_issue_status"},
			),
		):
			index_names = {
				str(row.Key_name) for row in frappe.db.sql(f"SHOW INDEX FROM `tab{doctype}`", as_dict=True)
			}
			self.assertTrue(expected.issubset(index_names))
		workspace = frappe.get_doc("Workspace", "HRP Master Data")
		shortcuts = {shortcut.link_to for shortcut in workspace.shortcuts}
		self.assertTrue({"HRP Data Quality Rule", "HRP Data Quality Issue"}.issubset(shortcuts))
		default_workspace_map = build_default_workspace_map(get_sidebar_items())
		self.assertEqual(default_workspace_map["HRP Data Quality Rule"], "HRP")
		self.assertEqual(default_workspace_map["HRP Data Quality Issue"], "HRP")

		direct = frappe.get_doc(
			{
				"doctype": "HRP Data Quality Rule",
				"master_data_domain": data_quality_domain(),
				"target_doctype": "Item",
				"code": "DIRECT",
				"display_name": "直接写入",
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"target_field": "item_name",
				"rule_type": "Required",
				"parameters_json": "{}",
				"severity": "Major",
				"enabled": 1,
				"valid_from": "2026-08-01",
				"revision": 1,
			}
		)
		with self.assertRaises(IoneApplicationError) as denied:
			direct.insert(ignore_permissions=True)
		self.assertEqual(denied.exception.code, "IONE-CORE-0008")

	def test_upsert_is_idempotent_revisioned_and_identity_is_immutable(self) -> None:
		create_data_quality_context(suffix="revision")
		command = rule_command()
		created = upsert_data_quality_rule(command, idempotency_key="COD-022-create")
		replay = upsert_data_quality_rule(command, idempotency_key="COD-022-create")
		self.assertEqual(created["revision"], 1)
		self.assertTrue(replay["idempotency_replayed"])
		updated = upsert_data_quality_rule(
			rule_command(
				rule_name=cast(str, created["name"]),
				expected_revision=1,
				display_name="物料名称长度（修订）",
			),
			idempotency_key="COD-022-update",
		)
		self.assertEqual(updated["revision"], 2)
		self.assertEqual(updated["changed_fields"], ["display_name"])
		with self.assertRaises(IoneApplicationError) as stale:
			upsert_data_quality_rule(
				rule_command(
					rule_name=cast(str, created["name"]),
					expected_revision=1,
					display_name="过期修订",
				),
				idempotency_key="COD-022-stale",
			)
		self.assertEqual(stale.exception.code, "IONE-CORE-0005")
		with self.assertRaises(IoneApplicationError) as immutable:
			upsert_data_quality_rule(
				rule_command(
					rule_name=cast(str, created["name"]),
					expected_revision=2,
					code="DIFFERENT",
				),
				idempotency_key="COD-022-identity",
			)
		self.assertEqual(immutable.exception.code, "IONE-CORE-0008")

	def test_failure_creates_issue_then_resolves_and_reopens(self) -> None:
		created = self._create_rule(suffix="lifecycle")
		failed_request = "COD-022-evaluate-fail"
		passed_request = "COD-022-evaluate-pass"
		failed = evaluate_data_quality(
			build_data_quality_evaluate(
				rule_name=created["name"],
				target_name=TEST_ITEM,
				effective_on="2026-08-08",
				expected_rule_revision=1,
			),
			idempotency_key=failed_request,
		)
		self.assertFalse(failed["passed"])
		self.assertEqual(failed["failure_code"], "MAXIMUM_LENGTH_EXCEEDED")
		issue_name = cast(dict[str, object], failed["issue"])["name"]
		replay = evaluate_data_quality(
			build_data_quality_evaluate(
				rule_name=created["name"],
				target_name=TEST_ITEM,
				effective_on="2026-08-08",
				expected_rule_revision=1,
			),
			idempotency_key=failed_request,
		)
		self.assertTrue(replay["idempotency_replayed"])
		self.assertEqual(frappe.db.get_value("HRP Data Quality Issue", issue_name, "occurrence_count"), 1)

		frappe.db.set_value("Item", TEST_ITEM, "item_name", "合格物料", update_modified=False)
		resolved = evaluate_data_quality(
			build_data_quality_evaluate(
				rule_name=created["name"],
				target_name=TEST_ITEM,
				effective_on="2026-08-08",
				expected_rule_revision=1,
			),
			idempotency_key=passed_request,
		)
		self.assertTrue(resolved["passed"])
		self.assertEqual(cast(dict[str, object], resolved["issue"])["name"], issue_name)
		self.assertEqual(cast(dict[str, object], resolved["issue"])["issue_status"], "Resolved")

		frappe.db.set_value("Item", TEST_ITEM, "item_name", TEST_ITEM, update_modified=False)
		reopened = evaluate_data_quality(
			build_data_quality_evaluate(
				rule_name=created["name"],
				target_name=TEST_ITEM,
				effective_on="2026-08-09",
				expected_rule_revision=1,
			),
			idempotency_key="COD-022-evaluate-reopen",
		)
		self.assertFalse(reopened["passed"])
		self.assertEqual(cast(dict[str, object], reopened["issue"])["name"], issue_name)
		self.assertEqual(cast(dict[str, object], reopened["issue"])["issue_status"], "Open")
		self.assertEqual(cast(dict[str, object], reopened["issue"])["occurrence_count"], 2)

	def test_scope_role_and_unknown_target_checks_precede_idempotency(self) -> None:
		create_data_quality_context(suffix="guards")
		before = frappe.db.count("HRP Service Idempotency")
		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			self.assertRaises(IoneApplicationError) as role_denied,
		):
			upsert_data_quality_rule(rule_command(), idempotency_key="COD-022-role-denied")
		self.assertEqual(role_denied.exception.code, "IONE-CORE-0002")
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)
		created = upsert_data_quality_rule(
			rule_command(),
			idempotency_key="COD-022-guard-rule",
		)
		with self.assertRaises(IoneApplicationError) as missing:
			evaluate_data_quality(
				build_data_quality_evaluate(
					rule_name=created["name"],
					target_name="COD022-MISSING",
					effective_on="2026-08-08",
					expected_rule_revision=1,
				),
				idempotency_key="COD-022-missing-target",
			)
		self.assertEqual(missing.exception.code, "IONE-CORE-0004")

	def test_database_issue_uniqueness_race_is_reported_as_conflict(self) -> None:
		created = self._create_rule(suffix="race")
		with (
			patch.object(
				HRPDataQualityIssue,
				"insert",
				side_effect=frappe.DuplicateEntryError("concurrent issue key"),
			),
			self.assertRaises(IoneApplicationError) as conflict,
		):
			evaluate_data_quality(
				build_data_quality_evaluate(
					rule_name=created["name"],
					target_name=TEST_ITEM,
					effective_on="2026-08-08",
					expected_rule_revision=1,
				),
				idempotency_key="COD-022-race",
			)
		self.assertEqual(conflict.exception.code, "IONE-CORE-0005")

	def test_permissions_and_audit_are_redacted(self) -> None:
		create_data_quality_context(suffix="audit")
		sentinel = "COD-022-sensitive-remarks"
		with patch("medops.services.audit_context.frappe.logger") as logger:
			created = upsert_data_quality_rule(
				rule_command(remarks=sentinel),
				idempotency_key="COD-022-audit-create",
			)
			evaluated = evaluate_data_quality(
				build_data_quality_evaluate(
					rule_name=created["name"],
					target_name=TEST_ITEM,
					effective_on="2026-08-08",
					expected_rule_revision=1,
				),
				idempotency_key="COD-022-audit-evaluate",
			)
		payload = json.dumps(
			[
				call.args[0]
				for level in (logger.return_value.info, logger.return_value.warning)
				for call in level.call_args_list
			],
			ensure_ascii=False,
		)
		self.assertNotIn(sentinel, payload)
		self.assertNotIn(TEST_ITEM, payload)
		request_record = frappe.get_doc(
			"HRP Service Idempotency",
			idempotency_record_name(
				UpsertDataQualityRuleService.definition.name,
				"COD-022-audit-create",
			),
		)
		self.assertNotIn(sentinel, request_record.request_fingerprint)
		issue = cast(dict[str, object], evaluated["issue"])
		self.assertNotIn("observed_value_digest", issue)
		self.assertNotIn("rule_digest", get_data_quality_issue(issue_name=issue["name"]))

		frappe.set_user(TEST_STEWARD)
		self.assertEqual(data_quality_query(), "")
		rule = frappe.get_doc("HRP Data Quality Rule", created["name"])
		self.assertTrue(can_read_data_quality(rule, ptype="read"))
		self.assertIn(created["name"], frappe.get_list("HRP Data Quality Rule", pluck="name"))
		frappe.set_user(TEST_PLAIN_USER)
		self.assertEqual(data_quality_query(), "1=0")
		self.assertFalse(can_read_data_quality(rule, ptype="read"))
		with self.assertRaises(IoneApplicationError) as forbidden:
			get_data_quality_issue(issue_name="missing")
		self.assertEqual(forbidden.exception.code, "IONE-CORE-0002")
		frappe.set_user("Guest")
		self.assertEqual(data_quality_query(), "1=0")
		self.assertFalse(can_read_data_quality(rule, ptype="read"))
		with self.assertRaises(IoneApplicationError) as unauthenticated:
			get_data_quality_issue(issue_name="missing")
		self.assertEqual(unauthenticated.exception.code, "IONE-CORE-0001")

	def test_scheduler_batches_rules_and_targets_without_unbounded_work(self) -> None:
		created = self._create_rule(suffix="schedule")
		with (
			patch(
				"medops.hrp_master_data.services.data_quality.frappe.get_all",
				return_value=[frappe._dict(name=created["name"], revision=1)],
			),
			patch("medops.hrp_master_data.services.data_quality.enqueue_with_audit") as enqueue,
		):
			report = run_data_quality_rules()
		self.assertEqual(report["scheduled_rule_count"], 1)
		enqueue.assert_called_once()

		targets = [frappe._dict(name=f"COD022-BATCH-{index:03}") for index in range(DATA_QUALITY_BATCH_SIZE)]
		with (
			patch(
				"medops.hrp_master_data.services.data_quality.frappe.get_all",
				return_value=targets,
			),
			patch(
				"medops.hrp_master_data.services.data_quality.evaluate_data_quality",
				return_value={"passed": True},
			) as evaluate,
			patch("medops.hrp_master_data.services.data_quality.enqueue_with_audit") as continue_enqueue,
		):
			batch = run_data_quality_rule_batch(
				rule_name=cast(str, created["name"]),
				effective_on="2026-08-08",
				expected_rule_revision=1,
			)
		self.assertEqual(batch, {"processed": DATA_QUALITY_BATCH_SIZE, "failed": 0, "has_more": True})
		self.assertEqual(evaluate.call_count, DATA_QUALITY_BATCH_SIZE)
		continue_enqueue.assert_called_once()
		self.assertEqual(continue_enqueue.call_args.kwargs["after_name"], targets[-1].name)


class TestDataQualityAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_data_quality_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_data_quality_state()
		create_data_quality_context(suffix="http")
		frappe.local.db.commit()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def _rule_payload(self) -> dict[str, object]:
		return {
			"code": "DQ-HTTP-ITEM-NAME",
			"display_name": "HTTP物料名称长度",
			"master_data_domain": data_quality_domain(),
			"company": TEST_COMPANY,
			"hospital": TEST_HOSPITAL,
			"target_field": "item_name",
			"rule_type": "Maximum Length",
			"parameters": {"maximum": 8},
			"valid_from": "2026-08-01",
			"expected_revision": 0,
		}

	def test_http_write_requires_idempotency_header(self) -> None:
		response = self.post(
			self.method(UPSERT_RULE_METHOD),
			self._rule_payload(),
			headers={"X-Correlation-ID": "COD-022-http-missing-key"},
		)
		self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
		self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0003")

	def test_http_rule_evaluation_and_issue_query(self) -> None:
		created_response = self.post(
			self.method(UPSERT_RULE_METHOD),
			self._rule_payload(),
			headers={
				"Idempotency-Key": "COD-022-http-rule",
				"X-Correlation-ID": "COD-022-http-rule",
			},
		)
		self.assertEqual(created_response.status_code, 200, created_response.get_data(as_text=True))
		created = created_response.get_json()["message"]
		evaluated_response = self.post(
			self.method(EVALUATE_METHOD),
			{
				"rule_name": created["name"],
				"target_name": TEST_ITEM,
				"effective_on": "2026-08-08",
				"expected_rule_revision": 1,
			},
			headers={
				"Idempotency-Key": "COD-022-http-evaluate",
				"X-Correlation-ID": "COD-022-http-evaluate",
			},
		)
		self.assertEqual(
			evaluated_response.status_code,
			200,
			evaluated_response.get_data(as_text=True),
		)
		evaluated = evaluated_response.get_json()["message"]
		self.assertFalse(evaluated["passed"])
		issue_response = self.get(
			self.method(GET_ISSUE_METHOD),
			{"issue_name": evaluated["issue"]["name"]},
			headers={"X-Correlation-ID": "COD-022-http-issue"},
		)
		self.assertEqual(issue_response.status_code, 200, issue_response.get_data(as_text=True))
		issue = issue_response.get_json()["message"]
		self.assertEqual(issue["issue_status"], "Open")
		self.assertNotIn("observed_value_digest", issue)
