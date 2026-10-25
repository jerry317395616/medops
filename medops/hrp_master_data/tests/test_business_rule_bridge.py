from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.business_rules import rule_payload
from medops.common.error_catalog import IoneApplicationError
from medops.common.master_data import build_master_data_request_submit
from medops.hrp_master_data.services.master_data import save_master_data_request, submit_master_data_request
from medops.hrp_master_data.tests.test_master_data import (
	TEST_COMPANY,
	TEST_HOSPITAL,
	create_context,
	create_domain,
	ensure_master_data_fixtures,
	request_command,
	reset_master_data_state,
)
from medops.hrp_workflow_authorization.services.business_rules import UpsertBusinessRule


def make_deduplication_identifier(label: str) -> str:
	return label


class TestMasterDataBusinessRules(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_master_data_fixtures()

	def setUp(self):
		super().setUp()
		reset_master_data_state()
		frappe.db.delete("HRP Rule Execution Log")
		frappe.db.delete("HRP Business Rule")
		frappe.db.delete("HRP Service Idempotency", {"service_name": UpsertBusinessRule.definition.name})

	def draft(self, *, severity="Block", operation="Create"):
		create_domain(suffix="rule-bridge")
		_, unit = create_context(suffix="rule-bridge")
		payload = rule_payload(
			{
				"code": "COD029-MASTER",
				"display_name": "允许的主数据动作",
				"target_doctype": "HRP Master Data Request",
				"target_action": "submit",
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"organization_unit": unit,
				"valid_from": "2026-01-01",
				"severity": severity,
				"assertion_json": {"field": "operation", "op": "eq", "value": operation},
			}
		)
		UpsertBusinessRule().execute(
			{"policy": payload, "expected_revision": 0},
			idempotency_key=make_deduplication_identifier("COD-029/master/policy"),
		)
		return save_master_data_request(
			request_command(unit), idempotency_key=make_deduplication_identifier("COD-029/master/draft")
		)

	def test_blocked_submission_rolls_back_source_and_does_not_claim_persisted_denial(self):
		draft = self.draft()
		with self.assertRaises(IoneApplicationError):
			submit_master_data_request(
				build_master_data_request_submit(
					request_name=draft["name"], expected_revision=draft["revision"]
				),
				idempotency_key=make_deduplication_identifier("COD-029/master/blocked"),
			)
		self.assertEqual(frappe.db.get_value("HRP Master Data Request", draft["name"], "docstatus"), 0)
		self.assertEqual(
			frappe.db.get_value("HRP Master Data Request", draft["name"], "request_status"), "Draft"
		)
		self.assertEqual(frappe.db.count("HRP Rule Execution Log"), 0)

	def test_warning_submission_records_guard_and_retry_does_not_submit_twice(self):
		draft = self.draft(severity="Warn")
		command = build_master_data_request_submit(
			request_name=draft["name"], expected_revision=draft["revision"]
		)
		first = submit_master_data_request(
			command, idempotency_key=make_deduplication_identifier("COD-029/master/warning")
		)
		repeat = submit_master_data_request(
			command, idempotency_key=make_deduplication_identifier("COD-029/master/warning")
		)
		self.assertEqual(first["request_status"], "Pending Review")
		self.assertTrue(repeat["idempotency_replayed"])
		self.assertEqual(
			frappe.db.count("HRP Rule Execution Log", {"purpose": "Guard", "outcome": "Warned"}), 1
		)
