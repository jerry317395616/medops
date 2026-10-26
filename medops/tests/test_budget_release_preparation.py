from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.budget_ledger import (
	LEDGER,
	RELEASE,
	RELEASE_ENTRY,
	build_release_request,
	build_revise_release_request,
)
from medops.common.business_rules import rule_payload
from medops.common.domain_service import idempotency_record_name
from medops.common.error_catalog import IoneApplicationError
from medops.common.integration_outbox import OUTBOX
from medops.hrp_budget.services.link_titles import get_link_title
from medops.hrp_budget.services.release import (
	CreateBudgetRelease,
	create_budget_release,
	revise_budget_release,
)
from medops.hrp_budget.services.release_access import get_release
from medops.hrp_workflow_authorization.services.business_rules import UpsertBusinessRule
from medops.tests import test_budget_consolidation as source_fixture
from medops.tests.test_budget_submission import fixture_request


def release_rule_fixtures():
	assert frappe.session.user == "Administrator" and source_fixture.COMPANY.startswith("COD-")
	for action in ("create", "amend", "release", "post"):
		code = "COD033-RELEASE-" + action.upper()
		if frappe.db.exists("HRP Business Rule", code):
			continue
		policy = rule_payload(
			dict(
				code=code,
				display_name="合成测试预算执行规则",
				target_doctype=RELEASE,
				target_action=action,
				company=source_fixture.COMPANY,
				hospital=source_fixture.HOSPITAL,
				organization_unit=None,
				valid_from="2026-01-01",
				severity="Block",
				assertion_json={
					"all": [
						{"field": "amount_schema_version", "op": "eq", "value": "2"},
						{"field": "line_count", "op": "gt", "value": "0"},
						{"field": "total_amount", "op": "gte", "value": "-999999999999999.999999999"},
						{"field": "total_amount", "op": "lte", "value": "999999999999999.999999999"},
					]
				},
			)
		)
		UpsertBusinessRule().execute(
			{"policy": policy, "expected_revision": 0}, idempotency_key=fixture_request("release-rule")
		)
	frappe.local.db.commit()


class TestBudgetReleasePreparation(FrappeAPITestCase):
	login_actor = source_fixture.TestBudgetConsolidationAPI.login_actor
	success = source_fixture.TestBudgetConsolidationAPI.success
	post_command = source_fixture.TestBudgetConsolidationAPI.post_command
	request_approval = source_fixture.TestBudgetConsolidationAPI.request_approval
	decide = source_fixture.TestBudgetConsolidationAPI.decide
	new_sources = source_fixture.TestBudgetConsolidationAPI.new_sources
	proposal = source_fixture.TestBudgetConsolidationAPI.proposal
	seal = source_fixture.TestBudgetConsolidationAPI.seal

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.org, cls.units, cls.value, cls.fund = source_fixture.fixtures()
		release_rule_fixtures()

	def test_real_approved_source_creates_and_revises_only_a_prepared_intent(self):
		self.sources = self.new_sources()
		name = self.success(self.seal())["name"]
		case = self.request_approval(name)
		self.login_actor(source_fixture.REVIEWER)
		self.success(self.decide(case))
		frappe.set_user(source_fixture.MANAGER)
		self.assertIsNone(frappe.db.get_value("HRP Hospital", source_fixture.HOSPITAL, "valid_from"))
		before = {
			doctype: frappe.db.count(doctype)
			for doctype in (LEDGER, RELEASE_ENTRY, OUTBOX, "GL Entry", "Stock Ledger Entry")
		}
		command = build_release_request(
			dict(
				approval_case=case,
				expected_approval_revision=2,
				posting_date="2026-10-26",
				reason="原始粒度预算下达准备",
			)
		)
		key = fixture_request("release-prepare")
		prepared_before = frappe.db.count(RELEASE)
		try:
			frappe.set_user("Administrator")
			rule = frappe.get_doc("HRP Business Rule", "COD033-RELEASE-CREATE")
			original_policy, _ = rule.runtime_definition()
			UpsertBusinessRule().execute(
				{
					"policy": rule_payload(dict(original_policy, enabled=False)),
					"expected_revision": int(rule.revision),
				},
				idempotency_key=fixture_request("release-rule-disable"),
			)
			frappe.set_user(source_fixture.MANAGER)
			blocked_key = fixture_request("release-missing-rule")
			with self.assertRaises(IoneApplicationError) as raised:
				create_budget_release(command, idempotency_key=blocked_key)
			self.assertEqual(raised.exception.code, "IONE-CORE-0006")
			self.assertEqual(prepared_before, frappe.db.count(RELEASE))
			self.assertFalse(
				frappe.db.exists(
					"HRP Service Idempotency",
					idempotency_record_name(CreateBudgetRelease.definition.name, blocked_key),
				)
			)
			frappe.set_user("Administrator")
			UpsertBusinessRule().execute(
				{"policy": original_policy, "expected_revision": int(rule.revision) + 1},
				idempotency_key=fixture_request("release-rule-restore"),
			)
			frappe.set_user(source_fixture.MANAGER)
			first = create_budget_release(command, idempotency_key=key)
			second = create_budget_release(command, idempotency_key=key)
			self.assertTrue(second["idempotency_replayed"])
			self.assertEqual(first["name"], second["name"])
			doc = get_release(first["name"])
			self.assertEqual(
				(doc.status, doc.docstatus, doc.total_amount), ("Prepared", 0, "100000.000000001")
			)
			self.assertEqual(doc.source_approval, case)
			self.assertEqual(doc.currency_precision, 9)
			self.assertEqual(doc.line_count, 2)
			frappe.set_user(source_fixture.MAKERS[0])
			with self.assertRaises(IoneApplicationError) as raised:
				get_link_title(RELEASE, first["name"])
			self.assertEqual(raised.exception.code, "IONE-CORE-0002")
			frappe.set_user(source_fixture.MANAGER)
			with patch.object(frappe.get_meta(RELEASE).get_field("total_amount"), "permlevel", 2):
				with self.assertRaises(IoneApplicationError) as raised:
					get_release(first["name"])
				self.assertEqual(raised.exception.code, "IONE-CORE-0002")
			doc.flags.ignore_permissions = True
			with self.assertRaises(IoneApplicationError) as raised:
				doc.submit()
			self.assertEqual(raised.exception.code, "IONE-CORE-0008")
			revised = revise_budget_release(
				build_revise_release_request(
					dict(
						release=first["name"],
						expected_revision=1,
						posting_date="2026-10-27",
						reason="修订尚未授权的准备意图",
					)
				),
				idempotency_key=fixture_request("release-revise"),
			)
			self.assertEqual(revised["revision"], 2)
			self.assertEqual(get_release(first["name"]).status, "Prepared")
			self.assertEqual(before, {doctype: frappe.db.count(doctype) for doctype in before})
		finally:
			frappe.db.rollback()
			frappe.set_user("Administrator")
