from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.approval_matrix import (
	build_approval_evaluation,
	build_approval_matrix_steps,
	build_approval_matrix_upsert,
)
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_workflow_authorization.services.approval_matrix import (
	evaluate_approval_matrix,
	get_approval_matrix,
	upsert_approval_matrix,
)
from medops.hrp_workflow_authorization.tests.test_approval_matrix import (
	TEST_COMPANY,
	TEST_HOSPITAL,
	TEST_UNIT,
	TEST_USER,
	ensure_approval_fixtures,
	evaluation_command,
	matrix_command,
)
from medops.setup.approval_matrix import ensure_approval_matrix_governance
from medops.tests.scope_fixtures import grant_test_scope

TARGET = "MedOps Exact Approval Test"
SOURCE = "COD032-EXACT-SOURCE"
AMOUNT = "999999999999.000000001"
METHOD_PREFIX = "medops.api.v1.core.approval."
GOLDEN = json.loads(
	(Path(__file__).resolve().parents[3] / "tests/fixtures/approval_v1_native_contract.json").read_text(
		encoding="utf-8"
	)
)


def key():
	return "COD032-" + uuid4().hex


def ensure_exact_fixtures():
	ensure_approval_fixtures()
	frappe.set_user("Administrator")
	if not frappe.db.exists("DocType", TARGET):
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": TARGET,
				"module": "Custom",
				"custom": 1,
				"autoname": "field:code",
				"fields": [
					{"fieldname": "code", "label": "Code", "fieldtype": "Data", "reqd": 1, "unique": 1},
					{"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company"},
					{
						"fieldname": "hospital",
						"label": "Hospital",
						"fieldtype": "Link",
						"options": "HRP Hospital",
					},
					{
						"fieldname": "organization_unit",
						"label": "Unit",
						"fieldtype": "Link",
						"options": "HRP Organization Unit",
					},
					{"fieldname": "posting_date", "label": "Date", "fieldtype": "Date"},
					{"fieldname": "total_amount", "label": "Amount", "fieldtype": "Data"},
					{
						"fieldname": "currency_precision",
						"label": "Precision",
						"fieldtype": "Int",
						"default": "9",
					},
				],
				"permissions": [
					{"role": "System Manager", "read": 1, "write": 1, "create": 1},
					{"role": "HRP User", "read": 1},
				],
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists(TARGET, SOURCE):
		frappe.get_doc(
			{
				"doctype": TARGET,
				"code": SOURCE,
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"organization_unit": TEST_UNIT,
				"posting_date": "2026-01-01",
				"total_amount": AMOUNT,
				"currency_precision": 9,
			}
		).insert(ignore_permissions=True)
	# The test fixture owns this transaction before per-case savepoints begin.
	frappe.local.db.commit()


def payload(*, code=None, precision=9, version=2, threshold=AMOUNT, priority=10):
	return {
		"code": code or key().upper(),
		"display_name": "精确审批测试",
		"target_doctype": TARGET,
		"company": TEST_COMPANY,
		"hospital": TEST_HOSPITAL,
		"organization_unit": TEST_UNIT,
		"valid_from": "2026-01-01",
		"priority": priority,
		"schema_version": version,
		"amount_precision": precision,
		"steps": [
			{
				"sequence_no": 1,
				"step_name": "Review",
				"threshold_amount": "0",
				"approver_type": "User",
				"approver_user": "Administrator",
				"approval_mode": "All",
			},
			{
				"sequence_no": 2,
				"step_name": "Final",
				"threshold_amount": threshold,
				"approver_type": "User",
				"approver_user": "Administrator",
				"approval_mode": "All",
			},
		],
	}


def command(**kwargs):
	data = payload(**kwargs)
	data["steps"] = build_approval_matrix_steps(
		data["steps"], schema_version=data["schema_version"], amount_precision=data["amount_precision"]
	)
	return build_approval_matrix_upsert(**data)


def evaluation_payload(amount=AMOUNT, precision=9, version=2):
	return {
		"doctype": TARGET,
		"docname": SOURCE,
		"amount": amount,
		"schema_version": version,
		"amount_precision": precision,
		"dimensions": {
			"company": TEST_COMPANY,
			"hospital": TEST_HOSPITAL,
			"organization_unit": TEST_UNIT,
			"effective_on": "2026-01-01",
		},
	}


def evaluate(**kwargs) -> dict[str, Any]:
	return cast(
		dict[str, Any], evaluate_approval_matrix(build_approval_evaluation(**evaluation_payload(**kwargs)))
	)


def save_matrix(request, *, idempotency_key) -> dict[str, Any]:
	return cast(dict[str, Any], upsert_approval_matrix(request, idempotency_key=idempotency_key))


def read_matrix(name: str) -> dict[str, Any]:
	return cast(dict[str, Any], get_approval_matrix(name))


def core(value):
	return {
		k: v for k, v in value.items() if k not in {"correlation_id", "request_id", "idempotency_replayed"}
	}


class TestExactApprovalMatrix(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_exact_fixtures()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.savepoint("cod032_exact")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="cod032_exact")
		frappe.clear_cache()
		super().tearDown()

	def test_precision_columns_index_and_native_storage_are_idempotent(self):
		for _ in range(2):
			ensure_approval_matrix_governance()
		self.assertEqual(
			frappe.get_meta("HRP Approval Matrix Row").get_field("threshold_amount").fieldtype, "Currency"
		)
		self.assertEqual(
			frappe.get_meta("HRP Approval Matrix Row").get_field("exact_threshold_amount").fieldtype, "Data"
		)
		created = save_matrix(command(), idempotency_key=key())
		doc = frappe.get_doc("HRP Approval Matrix", created["name"])
		self.assertEqual(doc.get("amount_schema_version"), 2)
		self.assertEqual(doc.get("amount_precision"), 9)
		self.assertEqual(doc.steps[1].get("exact_threshold_amount"), AMOUNT)
		self.assertEqual(doc.steps[1].threshold_amount, 0)
		self.assertEqual(created["steps"][1]["threshold_amount"], AMOUNT)
		indexes = {
			row.Key_name for row in frappe.db.sql("SHOW INDEX FROM `tabHRP Approval Matrix`", as_dict=True)
		}
		self.assertIn("idx_hrp_approval_matrix_amount_profile", indexes)

	def test_legacy_native_matrix_and_decision_match_preupgrade_golden(self):
		created = save_matrix(
			matrix_command(code="COD032-LEGACY-MIGRATION", priority=9999), idempotency_key=key()
		)
		self.assertEqual(core(read_matrix(created["name"])), GOLDEN["matrix"])
		self.assertEqual(core(evaluate_approval_matrix(evaluation_command())), GOLDEN["decision"])
		doc = frappe.get_doc("HRP Approval Matrix", created["name"])
		self.assertEqual(doc.get("amount_schema_version"), 1)
		self.assertEqual(doc.get("amount_precision"), 2)
		self.assertFalse(doc.steps[0].get("exact_threshold_amount"))

	def test_exact_evaluation_compares_native_source_and_selects_compatible_profile(self):
		save_matrix(command(precision=2, threshold="1", priority=1000), idempotency_key=key())
		legacy = save_matrix(
			command(version=1, precision=2, threshold="1", priority=5), idempotency_key=key()
		)
		exact = save_matrix(command(), idempotency_key=key())
		result = evaluate()
		self.assertEqual(result["matrix"], exact["name"])
		self.assertEqual(result["amount"], AMOUNT)
		self.assertEqual(len(result["steps"]), 2)
		frappe.db.set_value(TARGET, SOURCE, "total_amount", "999999999999.000000000")
		self.assertEqual(len(evaluate(amount="999999999999.000000000")["steps"]), 1)
		frappe.db.set_value(TARGET, SOURCE, "total_amount", "0")
		self.assertEqual(evaluate(amount="0", precision=2, version=1)["matrix"], legacy["name"])

	def test_exact_upsert_replays_revises_and_keeps_profile_identity(self):
		request = command()
		token = key()
		created = save_matrix(request, idempotency_key=token)
		self.assertTrue(save_matrix(request, idempotency_key=token)["idempotency_replayed"])
		data = payload(code=created["code"], threshold="1.000000001")
		data.update(matrix_name=created["name"], expected_revision=1)
		data["steps"] = build_approval_matrix_steps(data["steps"], schema_version=2, amount_precision=9)
		changed = save_matrix(build_approval_matrix_upsert(**data), idempotency_key=key())
		self.assertEqual(changed["revision"], 2)
		self.assertEqual(changed["steps"][1]["threshold_amount"], "1.000000001")
		data.update(amount_precision=2, expected_revision=2)
		data["steps"] = build_approval_matrix_steps(
			payload(threshold="1", precision=2)["steps"], schema_version=2, amount_precision=2
		)
		with self.assertRaises(IoneApplicationError):
			save_matrix(build_approval_matrix_upsert(**data), idempotency_key=key())
		self.assertEqual(read_matrix(created["name"])["revision"], 2)

	def test_exact_source_precision_and_corrupt_amount_fail_without_rounding(self):
		save_matrix(command(), idempotency_key=key())
		for kwargs in ({"precision": 2, "amount": "1"}, {"amount": "999999999999.000000000"}):
			with self.assertRaises(IoneApplicationError) as denied:
				evaluate(**kwargs)
			self.assertEqual(denied.exception.http_status_code, 409)
		frappe.db.set_value(TARGET, SOURCE, "total_amount", "1.0000000001")
		with self.assertRaises(IoneApplicationError) as denied:
			evaluate(amount="1")
		self.assertEqual(denied.exception.http_status_code, 500)
		field = frappe.db.get_value("DocField", {"parent": TARGET, "fieldname": "total_amount"}, "name")
		frappe.db.set_value("DocField", field, "fieldtype", "Float")
		frappe.clear_cache(doctype=TARGET)
		with self.assertRaises(IoneApplicationError):
			evaluate(amount="1")

	def test_scope_and_masked_fields_are_checked_before_source_values(self):
		save_matrix(command(), idempotency_key=key())
		grant_test_scope(
			code="COD032-EXACT-SCOPE", company=TEST_COMPANY, hospital=TEST_HOSPITAL, users=(TEST_USER,)
		)
		frappe.set_user(TEST_USER)
		self.assertEqual(evaluate()["amount"], AMOUNT)
		frappe.set_user("Administrator")
		field = frappe.db.get_value("DocField", {"parent": TARGET, "fieldname": "total_amount"}, "name")
		frappe.db.set_value("DocField", field, "mask", 1)
		frappe.clear_cache(doctype=TARGET)
		frappe.set_user(TEST_USER)
		with self.assertRaises(IoneApplicationError) as denied:
			evaluate(amount="1")
		self.assertEqual(denied.exception.http_status_code, 403)
		frappe.set_user("Administrator")
		frappe.db.set_value("DocField", field, "mask", 0)
		frappe.clear_cache(doctype=TARGET)
		frappe.db.set_value("HRP Access Scope", "COD032-EXACT-SCOPE", "enabled", 0)
		frappe.set_user(TEST_USER)
		with self.assertRaises(IoneApplicationError) as denied:
			evaluate(amount="1")
		self.assertEqual(denied.exception.http_status_code, 403)

	def test_native_form_cannot_mix_threshold_storage_contracts(self):
		created = save_matrix(command(), idempotency_key=key())
		doc = frappe.get_doc("HRP Approval Matrix", created["name"])
		doc.steps[1].threshold_amount = 1
		with self.assertRaises(IoneApplicationError):
			doc.save(ignore_permissions=True)
		self.assertEqual(read_matrix(created["name"])["steps"][1]["threshold_amount"], AMOUNT)


class TestExactApprovalAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_exact_fixtures()

	def setUp(self):
		super().setUp()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def test_http_exact_roundtrip_and_strict_inputs(self):
		data = payload()
		created = self.post(self.method(METHOD_PREFIX + "upsert"), data, headers={"Idempotency-Key": key()})
		self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
		response = self.post(self.method(METHOD_PREFIX + "evaluate"), evaluation_payload())
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		self.assertEqual(response.get_json()["message"]["amount"], AMOUNT)
		for invalid in (
			{**evaluation_payload(), "amount": 1.0},
			{**evaluation_payload(), "script": "ignored?"},
		):
			self.assertEqual(self.post(self.method(METHOD_PREFIX + "evaluate"), invalid).status_code, 400)
		data["steps"][1]["threshold_amount"] = 1.0
		self.assertEqual(
			self.post(
				self.method(METHOD_PREFIX + "upsert"), data, headers={"Idempotency-Key": key()}
			).status_code,
			400,
		)

	def test_http_guest_rejected_before_missing_or_malformed_arguments(self):
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		for method in ("upsert", "evaluate"):
			for data in ({}, {"schema_version": [], "amount_precision": 999, "amount": {}}):
				self.assertEqual(self.post(self.method(METHOD_PREFIX + method), data).status_code, 401)
