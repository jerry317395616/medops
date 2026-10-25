from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase
from frappe.utils import getdate, now_datetime, today

from medops.common.core_reports import QUALITY_EVENT, REFERENCE, REPORTS
from medops.common.data_quality import build_data_quality_evaluate
from medops.common.error_catalog import IoneApplicationError
from medops.common.external_code_mapping import build_external_code_mapping_upsert
from medops.common.master_data import build_master_data_request_review, build_master_data_request_submit
from medops.common.organization import (
	build_hierarchy_replace,
	build_organization_version_create,
	build_organization_version_publish,
)
from medops.hrp_foundation.services.reference_links import register_references, visible_references
from medops.hrp_master_data.services.data_quality import (
	evaluate_data_quality,
	upsert_data_quality_rule,
)
from medops.hrp_master_data.services.external_code_mapping import upsert_external_code_mapping
from medops.hrp_master_data.services.master_data import (
	review_master_data_request,
	save_master_data_request,
	submit_master_data_request,
)
from medops.hrp_master_data.services.quality_history import capture_epoch, ensure_quality_history
from medops.hrp_master_data.tests.test_access_boundaries import (
	ACTOR,
	PEER,
	ensure_access_fixtures,
	grant_scope,
	private_item_name,
	reset_access_state,
	share_item,
)
from medops.hrp_master_data.tests.test_data_quality import (
	TEST_COMPANY,
	TEST_HOSPITAL,
	TEST_ITEM,
	data_quality_domain,
	rule_command,
)
from medops.hrp_master_data.tests.test_master_data import TEST_COMPANY as OTHER_COMPANY
from medops.hrp_master_data.tests.test_master_data import TEST_HOSPITAL as OTHER_HOSPITAL
from medops.hrp_master_data.tests.test_master_data import hierarchy_nodes, request_command
from medops.hrp_organization.services.organization import (
	create_organization_version,
	get_organization_hierarchy,
	publish_organization_version,
	replace_organization_hierarchy,
)
from medops.services.core_reports import execute_core_report, export_query
from medops.services.core_workbench import workbench
from medops.setup.core_reports import ensure_core_reports

SECOND = "COD030-SECOND"
API = "medops.api.v1.core_workbench."


def key(label):
	return "COD030-" + label


def setup_context():
	version, unit = reset_access_state()
	frappe.db.delete(REFERENCE, {"hospital": ("in", [TEST_HOSPITAL, OTHER_HOSPITAL])})
	frappe.db.delete("HRP Service Idempotency", {"service_name": "hrp_foundation.references.register"})
	if not frappe.db.exists("Item", SECOND):
		item = frappe.get_doc("Item", TEST_ITEM)
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": SECOND,
				"item_name": "ABC 药品",
				"item_group": item.item_group,
				"stock_uom": item.stock_uom,
				"is_stock_item": 0,
			}
		).insert(ignore_permissions=True)
	frappe.db.set_value("Item", TEST_ITEM, "item_name", "ＡＢＣ　药品")
	frappe.db.set_value("Item", SECOND, "item_name", "abc 药品")
	return version, unit


def context():
	return {"company": TEST_COMPANY, "hospital": TEST_HOSPITAL, "master_data_domain": data_quality_domain()}


def register(*names):
	return register_references(
		context(),
		[{"target_name": name, "valid_from": "2026-01-01"} for name in names],
		idempotency_key=key("register-" + "-".join(names)),
	)


def report(code, filters=None, export=False):
	return execute_core_report(code, filters or {}, export=export)


def create_quality():
	rule = upsert_data_quality_rule(
		rule_command(code="COD030-QUALITY", parameters={"maximum": 2}), idempotency_key=key("quality-rule")
	)
	command = build_data_quality_evaluate(
		rule_name=rule["name"], target_name=TEST_ITEM, effective_on=today(), expected_rule_revision=1
	)
	result = evaluate_data_quality(command, idempotency_key=key("quality-first"))
	return rule, command, cast(dict[str, Any], result["issue"])


class TestCoreReports(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_access_fixtures()

	def setUp(self):
		super().setUp()
		self.version, self.unit = setup_context()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_models_eight_real_reports_indexes_and_repeat_migration(self):
		self.assertEqual(len(REPORTS), 8)
		for name, _, _, _ in REPORTS.values():
			doc = frappe.get_doc("Report", name)
			self.assertEqual(doc.report_type, "Script Report")
			self.assertFalse(doc.prepared_report)
			doc.prepared_report = 1
			with patch.dict(frappe.conf, {"developer_mode": 1}):
				with self.assertRaises(IoneApplicationError):
					doc.save(ignore_permissions=True)
		self.assertTrue(capture_epoch())
		before = frappe.db.count(QUALITY_EVENT)
		self.assertEqual(ensure_core_reports()["baselines"], 0)
		self.assertEqual(ensure_core_reports()["baselines"], 0)
		self.assertEqual(frappe.db.count(QUALITY_EVENT), before)

	def test_registration_is_atomic_revisioned_and_replayed(self):
		created = register(TEST_ITEM)
		self.assertEqual(created["items"][0]["revision"], 1)
		self.assertTrue(register(TEST_ITEM)["idempotency_replayed"])
		updated = register_references(
			context(),
			[
				{
					"target_name": TEST_ITEM,
					"valid_from": "2026-01-01",
					"enabled": False,
					"expected_revision": 1,
				}
			],
			idempotency_key=key("disable"),
		)
		self.assertEqual(updated["items"][0]["revision"], 2)
		with self.assertRaises(IoneApplicationError):
			register_references(
				context(),
				[
					{"target_name": SECOND, "valid_from": "2026-01-01"},
					{"target_name": "missing", "valid_from": "2026-01-01"},
				],
				idempotency_key=key("atomic"),
			)
		self.assertEqual(frappe.db.count(REFERENCE, {"target_name": SECOND}), 0)
		with self.assertRaises(IoneApplicationError):
			register_references(
				context(),
				[
					{
						"target_name": TEST_ITEM,
						"valid_from": "2026-01-01",
						"enabled": True,
						"expected_revision": 1,
					}
				],
				idempotency_key=key("stale"),
			)

	def test_later_batch_conflict_rolls_back_earlier_revision_and_idempotency(self):
		register(TEST_ITEM, SECOND)
		first, second = sorted((TEST_ITEM, SECOND))
		before = frappe.db.count("HRP Service Idempotency")
		with self.assertRaises(IoneApplicationError):
			register_references(
				context(),
				[
					{
						"target_name": first,
						"valid_from": "2026-01-01",
						"enabled": False,
						"expected_revision": 1,
					},
					{"target_name": second, "valid_from": "2026-01-01", "expected_revision": 0},
				],
				idempotency_key=key("late-conflict"),
			)
		rows = visible_references(context())
		self.assertEqual(len(rows), 2)
		self.assertTrue(all(row.enabled and row.revision == 1 for row in rows))
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)

	def test_direct_registration_and_event_mutation_are_rejected(self):
		row = register(TEST_ITEM)["items"][0]
		doc = frappe.get_doc(REFERENCE, row["name"])
		doc.enabled = 0
		with self.assertRaises(IoneApplicationError):
			doc.save(ignore_permissions=True)
		_, _, issue = create_quality()
		event = frappe.get_doc(QUALITY_EVENT, {"issue": issue["name"]})
		event.to_status = "Resolved"
		with self.assertRaises(IoneApplicationError):
			event.save(ignore_permissions=True)
		with self.assertRaises(IoneApplicationError):
			event.delete(ignore_permissions=True)

	def test_duplicate_population_is_explicit_and_normalized(self):
		self.assertEqual(report("RPT-004", context())[1], [])
		register(TEST_ITEM)
		self.assertEqual(report("RPT-004", context())[1], [])
		register(SECOND)
		rows = report("RPT-004", context())[1]
		self.assertEqual({row["target_name"] for row in rows}, {TEST_ITEM, SECOND})
		self.assertTrue(all(row["duplicate_count"] == 2 for row in rows))

	def test_completeness_uses_registered_visible_denominator_and_real_mappings(self):
		register(TEST_ITEM, SECOND)
		upsert_external_code_mapping(
			build_external_code_mapping_upsert(
				master_data_domain=data_quality_domain(),
				company=TEST_COMPANY,
				hospital=TEST_HOSPITAL,
				external_system="HIS",
				external_code="001A",
				internal_name=TEST_ITEM,
				valid_from="2026-01-01",
				expected_revision=0,
			),
			idempotency_key=key("mapping"),
		)
		row = report("RPT-005", {**context(), "external_system": "HIS"})[1][0]
		self.assertEqual((row["population"], row["mapped"], row["missing"], row["percent"]), (2, 1, 1, 50))
		frappe.db.delete(REFERENCE, {"hospital": TEST_HOSPITAL})
		self.assertIsNone(report("RPT-005", {**context(), "external_system": "HIS"})[1][0]["percent"])

	def test_native_scope_fields_and_generic_query_cannot_leak_population(self):
		register(TEST_ITEM, SECOND)
		grant_scope()
		frappe.set_user(ACTOR)
		self.assertEqual(visible_references(context()), [])
		frappe.set_user("Administrator")
		share_item()
		share_item(target=SECOND)
		frappe.set_user(ACTOR)
		self.assertEqual(len(report("RPT-004", context())[1]), 2)
		self.assertEqual(frappe.get_list(REFERENCE, pluck="name"), [])
		with private_item_name():
			self.assertEqual(report("RPT-004", context())[1], [])

	def test_cross_hospital_and_live_export_revocation(self):
		register(TEST_ITEM, SECOND)
		scope = grant_scope()
		share_item()
		share_item(target=SECOND)
		frappe.get_doc("User", ACTOR).add_roles("Stock User")
		frappe.set_user(ACTOR)
		self.assertEqual(len(report("RPT-004", context(), export=True)[1]), 2)
		frappe.set_user("Administrator")
		frappe.db.set_value("HRP Access Scope Member", scope.members[0].name, "allow_export", 0)
		frappe.set_user(ACTOR)
		self.assertEqual(report("RPT-004", context(), export=True)[1], [])
		self.assertEqual(len(report("RPT-004", context())[1]), 2)
		frappe.set_user("Administrator")
		frappe.db.set_value("HRP Access Scope", scope.name, "enabled", 0)
		frappe.set_user(ACTOR)
		self.assertEqual(report("RPT-004", context())[1], [])

	def test_background_email_export_rechecks_revoked_export_without_http_marker(self):
		from frappe.desk.query_report import run_export_query_job

		register(TEST_ITEM, SECOND)
		scope = grant_scope()
		share_item()
		share_item(target=SECOND)
		frappe.get_doc("User", ACTOR).add_roles("Stock User")
		frappe.db.set_value("HRP Access Scope Member", scope.members[0].name, "allow_export", 0)
		frappe.set_user(ACTOR)
		self.assertEqual(len(report("RPT-004", context())[1]), 2)
		params = frappe._dict(report_name=REPORTS["RPT-004"][0], filters=context(), file_format_type="CSV")
		with patch("frappe.desk.utils.send_report_email") as send:
			run_export_query_job("cod030-fixture@example.invalid", params, {})
			send.assert_called_once()
			content = send.call_args.args[3]
			if isinstance(content, bytes):
				content = content.decode("utf-8-sig")
			self.assertNotIn(TEST_ITEM, content)
			self.assertNotIn(SECOND, content)

	def test_history_tracks_real_transitions_and_unknown_days(self):
		_, command, issue = create_quality()
		evaluate_data_quality(command, idempotency_key=key("repeat"))
		frappe.db.set_value("Item", TEST_ITEM, "item_name", "A")
		evaluate_data_quality(command, idempotency_key=key("resolve"))
		rows = report(
			"RPT-006",
			{
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"from_date": str(getdate(today()) - timedelta(days=1)),
				"to_date": str(getdate(today()) + timedelta(days=1)),
			},
		)[1]
		self.assertIsNone(rows[0]["opened"])
		self.assertEqual(rows[1]["opened"], 1)
		self.assertEqual(rows[1]["repeated"], 1)
		self.assertEqual(rows[1]["resolved"], 1)
		self.assertEqual(rows[1]["open_at_end"], 0)
		self.assertIsNone(rows[2]["open_at_end"])
		self.assertEqual(frappe.db.count(QUALITY_EVENT, {"issue": issue["name"]}), 3)

	def test_event_failure_rolls_back_issue_and_idempotency(self):
		rule = upsert_data_quality_rule(
			rule_command(code="COD030-ROLLBACK", parameters={"maximum": 1}),
			idempotency_key=key("rollback-rule"),
		)
		with patch(
			"medops.hrp_master_data.services.quality_history.append_event",
			side_effect=RuntimeError("test rollback"),
		):
			with self.assertRaises(IoneApplicationError):
				evaluate_data_quality(
					build_data_quality_evaluate(
						rule_name=rule["name"],
						target_name=TEST_ITEM,
						effective_on=today(),
						expected_rule_revision=1,
					),
					idempotency_key=key("rollback-evaluate"),
				)
		self.assertEqual(frappe.db.count("HRP Data Quality Issue", {"quality_rule": rule["name"]}), 0)
		self.assertEqual(
			frappe.db.count(
				"HRP Service Idempotency", {"service_name": "hrp_master_data.data_quality.evaluate"}
			),
			0,
		)

	def test_baseline_does_not_invent_past_detections_and_repeats_once(self):
		_, _, issue = create_quality()
		before = frappe.get_doc("HRP Data Quality Issue", issue["name"]).as_public_dict()
		frappe.db.delete(QUALITY_EVENT, {"issue": issue["name"]})
		self.assertEqual(ensure_quality_history()["baselines"], 1)
		self.assertEqual(ensure_quality_history()["baselines"], 0)
		event = frappe.get_doc(QUALITY_EVENT, {"issue": issue["name"]})
		self.assertEqual(event.event_type, "Baseline")
		self.assertEqual(frappe.get_doc("HRP Data Quality Issue", issue["name"]).as_public_dict(), before)

	def test_history_permissions_recheck_current_source_and_event_fields(self):
		create_quality()
		grant_scope()
		share_item()
		frappe.set_user(ACTOR)
		filters = {
			"company": TEST_COMPANY,
			"hospital": TEST_HOSPITAL,
			"from_date": today(),
			"to_date": today(),
		}
		self.assertEqual(report("RPT-006", filters)[1][0]["opened"], 1)
		with private_item_name():
			self.assertEqual(report("RPT-006", filters)[1][0]["opened"], 0)
		from medops.hrp_master_data.tests.test_access_boundaries import private_field

		with private_field(QUALITY_EVENT, "occurred_at"):
			with self.assertRaises(IoneApplicationError):
				report("RPT-006", filters)

	def test_organization_difference_uses_complete_large_published_snapshots(self):
		version = create_organization_version(
			build_organization_version_create(
				hospital=OTHER_HOSPITAL, effective_from="2026-09-01", version_label="第二版"
			),
			idempotency_key=key("version"),
		)
		nodes = hierarchy_nodes()
		nodes[1]["display_name"] = "修订门诊"
		nodes.extend(
			{
				"code": f"COD030-UNIT-{i}",
				"display_name": f"测试科室{i}",
				"unit_type": "CLINICAL_DEPARTMENT",
				"parent_code": OTHER_HOSPITAL,
				"is_group": False,
				"enabled": True,
				"sequence": i + 3,
			}
			for i in range(25)
		)
		replaced = replace_organization_hierarchy(
			build_hierarchy_replace(organization_version=version["name"], expected_revision=1, nodes=nodes),
			idempotency_key=key("nodes"),
		)
		publish_organization_version(
			build_organization_version_publish(
				organization_version=version["name"], expected_revision=replaced["revision"]
			),
			idempotency_key=key("publish"),
		)
		self.assertEqual(
			len(
				cast(
					list[Any], get_organization_hierarchy(organization_version=str(version["name"]))["nodes"]
				)
			),
			len(nodes),
		)
		rows = report("RPT-001", {"from_version": self.version["name"], "to_version": version["name"]})[1]
		self.assertEqual(sum(row["change"] == "Added" for row in rows), 25)
		self.assertTrue(any(row["change"] == "Changed" for row in rows))

	def test_mapping_report_reads_real_standard_labels(self):
		from medops.hrp_organization.services.organization_mapping import upsert_organization_mapping
		from medops.hrp_organization.tests.test_organization_mapping import (
			create_version,
			ensure_company_fixture,
			mapping_command,
			reset_mapping_test_state,
		)

		ensure_company_fixture()

		reset_mapping_test_state()
		version = create_version(suffix="cod030")
		mapping = upsert_organization_mapping(
			mapping_command(str(version["name"]), "OUTPATIENT"), idempotency_key=key("org-map")
		)
		rows = report("RPT-002", {"organization_version": version["name"]})[1]
		row = next(row for row in rows if row["name"] == mapping["name"])
		self.assertEqual(
			row["department_label"], frappe.db.get_value("Department", row["department"], "department_name")
		)

	def test_turnaround_uses_real_submission_and_review(self):
		grant_scope(PEER, company=OTHER_COMPANY, hospital=OTHER_HOSPITAL)
		share_item(PEER)
		draft = save_master_data_request(
			request_command(self.unit, target_name=TEST_ITEM), idempotency_key=key("draft")
		)
		stamp = now_datetime()
		with patch("medops.hrp_master_data.services.master_data.now_datetime", return_value=stamp):
			submitted = submit_master_data_request(
				build_master_data_request_submit(request_name=draft["name"], expected_revision=1),
				idempotency_key=key("submit"),
			)
		frappe.set_user(PEER)
		with patch(
			"medops.hrp_master_data.services.master_data.now_datetime",
			return_value=stamp + timedelta(minutes=2),
		):
			review_master_data_request(
				build_master_data_request_review(
					request_name=draft["name"],
					expected_revision=submitted["revision"],
					decision="Approve",
					reason="测试复核",
				),
				idempotency_key=key("review"),
			)
		self.assertEqual(
			next(row for row in report("RPT-003")[1] if row["name"] == draft["name"])["elapsed_minutes"], 2
		)

	def test_delegation_and_segregation_reports_use_actual_services(self):
		from medops.hrp_workflow_authorization.services.segregation import upsert_segregation_rule
		from medops.hrp_workflow_authorization.tests.test_approval_matrix import (
			TEST_UNIT,
			ensure_approval_fixtures,
		)
		from medops.hrp_workflow_authorization.tests.test_segregation import (
			reset_segregation_state,
		)
		from medops.hrp_workflow_authorization.tests.test_segregation import (
			rule_command as sod_rule,
		)

		ensure_approval_fixtures()
		reset_segregation_state()
		upsert_segregation_rule(sod_rule(), idempotency_key=key("sod-rule"))
		row = report(
			"RPT-008",
			{
				"subject_user": "Administrator",
				"action": "approve",
				"target_doctype": "HRP Organization Unit",
				"docname": TEST_UNIT,
			},
		)[1][0]
		self.assertFalse(row["allowed"])
		self.assertGreater(row["conflict_count"], 0)
		self.assertIsInstance(report("RPT-007")[1], list)

	def test_workbench_returns_real_owned_todos_requests_issues_and_sla(self):
		create_quality()
		draft = save_master_data_request(request_command(self.unit), idempotency_key=key("pending-draft"))
		submit_master_data_request(
			build_master_data_request_submit(request_name=draft["name"], expected_revision=1),
			idempotency_key=key("pending-submit"),
		)
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"allocated_to": "Administrator",
				"description": "COD030 private description",
				"status": "Open",
				"reference_type": "HRP Master Data Request",
				"reference_name": draft["name"],
			}
		).insert(ignore_permissions=True)
		from medops.hrp_workflow_authorization.services.sla import UpsertPolicy
		from medops.hrp_workflow_authorization.tests.test_sla import (
			create_source,
			ensure_sla_fixtures,
			policy,
			reset_sla,
		)

		ensure_sla_fixtures()
		reset_sla()
		UpsertPolicy().execute(
			{"policy": policy(), "expected_revision": 0}, idempotency_key=key("sla-policy")
		)
		create_source()
		data = workbench()
		self.assertTrue(data["counts_are_preview_only"])
		self.assertIn(todo.name, [row["name"] for row in data["sections"]["todos"]["items"]])
		self.assertNotIn("COD030 private description", str(data))
		self.assertTrue(data["sections"]["requests"]["items"])
		self.assertTrue(data["sections"]["issues"]["items"])
		self.assertTrue(data["sections"]["sla"]["items"])

	def test_export_adapter_marks_sync_and_background_but_leaves_other_reports(self):
		before = frappe.local.form_dict
		try:
			for background in (0, 1):
				frappe.local.form_dict = frappe._dict(
					report_name=REPORTS["RPT-004"][0],
					filters=json.dumps(context()),
					export_in_background=background,
				)
				with patch("frappe.desk.query_report.export_query") as original:
					export_query()
					original.assert_called_once()
				self.assertTrue(json.loads(frappe.form_dict.filters)["_medops_export"])
			frappe.local.form_dict = frappe._dict(report_name="General Ledger", filters="{}")
			with patch("frappe.desk.query_report.export_query") as original:
				export_query()
				original.assert_called_once()
			self.assertEqual(frappe.form_dict.filters, "{}")
		finally:
			frappe.local.form_dict = before

	def test_filters_and_excess_population_cannot_produce_fake_totals(self):
		with self.assertRaises(IoneApplicationError):
			report("RPT-004", {**context(), "subject_user": "Administrator"})
		with patch(
			"medops.services.core_report_access.frappe.get_list", return_value=[frappe._dict(name="x")] * 5001
		):
			with self.assertRaises(IoneApplicationError):
				report("RPT-004", context())


class TestCoreWorkbenchAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_access_fixtures()

	def setUp(self):
		super().setUp()
		setup_context()
		frappe.local.db.commit()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def test_guest_denied_before_parsing_and_unknown_arguments_rejected(self):
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		for method in ("overview", "report", "population"):
			response = self.get(self.method(API + method))
			self.assertEqual(response.status_code, 401, response.get_data(as_text=True))
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)
		self.assertEqual(self.get(self.method(API + "overview"), {"user": "Administrator"}).status_code, 400)

	def test_register_requires_key_and_lists_actual_scope(self):
		payload = {"scope": context(), "entries": [{"target_name": TEST_ITEM, "valid_from": "2026-01-01"}]}
		self.assertEqual(self.post(self.method(API + "register_population"), payload).status_code, 400)
		response = self.post(
			self.method(API + "register_population"),
			payload,
			headers={"Idempotency-Key": key("http-register")},
		)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		listed = self.get(self.method(API + "population"), {"filters": json.dumps(context())})
		self.assertEqual(listed.status_code, 200)
		self.assertEqual(len(listed.get_json()["message"]["items"]), 1)

	def test_overview_report_and_standard_script_report_endpoints(self):
		response = self.get(self.method(API + "overview"))
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		self.assertEqual(len(response.get_json()["message"]["reports"]), 8)
		response = self.get(
			self.method(API + "report"), {"code": "RPT-004", "filters": json.dumps(context())}
		)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		self.assertEqual(response.get_json()["message"]["rows"], [])
		response = self.get(
			self.method("frappe.desk.query_report.run"),
			{"report_name": REPORTS["RPT-004"][0], "filters": json.dumps(context())},
		)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

	def test_actual_csv_alias_and_background_export_keep_current_checks(self):
		for target in (TEST_ITEM, SECOND):
			response = self.post(
				self.method(API + "register_population"),
				{"scope": context(), "entries": [{"target_name": target, "valid_from": "2026-01-01"}]},
				headers={"Idempotency-Key": key("csv-" + target)},
			)
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		params = {
			"report_name": REPORTS["RPT-004"][0].lower(),
			"filters": json.dumps(context()),
			"file_format_type": "CSV",
			"export_in_background": 0,
		}
		response = self.post(self.method("frappe.desk.query_report.export_query"), params)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		self.assertIn(TEST_ITEM, response.get_data(as_text=True))
		self.assertIn(SECOND, response.get_data(as_text=True))
		with patch("frappe.desk.query_report.frappe.enqueue") as enqueue:
			response = self.post(
				self.method("frappe.desk.query_report.export_query"), {**params, "export_in_background": 1}
			)
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
			self.assertTrue(enqueue.call_args.kwargs["form_params"]["filters"]["_medops_export"])

	def test_legacy_custom_prepared_cache_is_disabled_without_changing_saved_config(self):
		name = "COD030 CUSTOM " + frappe.generate_hash(length=8)
		frappe.get_doc(
			{
				"doctype": "Report",
				"report_name": name,
				"name": name,
				"report_type": "Custom Report",
				"is_standard": "No",
				"module": "HRP Foundation",
				"ref_doctype": REFERENCE,
				"reference_report": REPORTS["RPT-004"][0],
				"json": "{}",
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value("Report", name, "prepared_report", 1)
		frappe.local.db.commit()
		response = self.get(
			self.method("frappe.desk.query_report.run"),
			{"report_name": name, "filters": json.dumps(context())},
		)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		self.assertFalse(response.get_json()["message"].get("prepared_report"))
		self.assertIn("result", response.get_json()["message"])
		self.assertEqual(frappe.db.get_value("Report", name, "prepared_report"), 1)

	def test_custom_link_columns_cannot_add_unreviewed_sources(self):
		response = self.get(
			self.method("frappe.desk.query_report.run"),
			{
				"report_name": REPORTS["RPT-004"][0],
				"filters": json.dumps(context()),
				"custom_columns": json.dumps(
					[{"fieldname": "item_name", "doctype": "Item", "link_field": "target_name"}]
				),
			},
		)
		self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
