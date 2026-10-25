from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.master_data import build_master_data_request_submit
from medops.common.sla import policy_payload
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
from medops.hrp_workflow_authorization.services.sla import UpsertPolicy


def make_deduplication_identifier(label: str) -> str:
	"""Public deterministic test identifiers, distinct from authentication credentials."""
	return label


class TestMasterDataSLABridge(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_master_data_fixtures()

	def test_real_master_data_submission_starts_governed_cycle(self):
		reset_master_data_state()
		create_domain(suffix="sla")
		_, unit = create_context(suffix="sla")
		payload = policy_payload(
			{
				"code": "COD028-MD-SLA",
				"display_name": "主数据申请时限",
				"target_doctype": "HRP Master Data Request",
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"organization_unit": unit,
				"valid_from": "2026-01-01",
				"status_field": "request_status",
				"start_field": "submitted_at",
				"response_field": None,
				"start_statuses_json": ["Pending Review"],
				"pause_statuses_json": [],
				"completion_statuses_json": ["Approved", "Rejected"],
				"response_seconds": 3600,
				"resolution_seconds": 7200,
				"clock_basis": "Elapsed",
				"responsible_user": "Administrator",
				"escalation_steps_json": [{"level": 1, "delay_seconds": 0, "assigned_to": "Administrator"}],
			}
		)
		UpsertPolicy().execute(
			{"policy": payload, "expected_revision": 0},
			idempotency_key=make_deduplication_identifier("COD028-real-policy"),
		)
		draft = save_master_data_request(
			request_command(unit), idempotency_key=make_deduplication_identifier("COD028-real-draft")
		)
		self.assertFalse(frappe.db.count("HRP SLA Escalation", {"reference_name": draft["name"]}))
		submit_master_data_request(
			build_master_data_request_submit(request_name=draft["name"], expected_revision=draft["revision"]),
			idempotency_key=make_deduplication_identifier("COD028-real-submit"),
		)
		self.assertEqual(
			frappe.db.count(
				"HRP SLA Escalation",
				{
					"reference_doctype": "HRP Master Data Request",
					"reference_name": draft["name"],
					"status": "Open",
				},
			),
			1,
		)
