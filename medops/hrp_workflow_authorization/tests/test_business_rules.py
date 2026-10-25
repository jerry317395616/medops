from __future__ import annotations

import json
from typing import Any, cast

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.business_rules import rule_payload
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_workflow_authorization.services.business_rules import (
	LOG,
	RULE,
	EvaluateBusinessRules,
	RecordBusinessRuleSnapshot,
	UpsertBusinessRule,
	enforce_business_rules,
	get_log,
	list_logs,
)
from medops.hrp_workflow_authorization.tests.test_approval_matrix import (
	TEST_COMPANY,
	TEST_HOSPITAL,
	TEST_UNIT,
	TEST_USER,
	ensure_approval_fixtures,
)
from medops.setup.business_rules import ensure_business_rule_governance

SOURCE = "MedOps Rule Test Request"
CHILD = "MedOps Rule Test Item"
API = "medops.api.v1.core.rules."


def deduplication_identifier(label: str) -> str:
	return "COD-029-" + label


def ensure_rule_fixtures() -> None:
	ensure_approval_fixtures()
	if not frappe.db.exists("DocType", CHILD):
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": CHILD,
				"module": "Custom",
				"custom": 1,
				"istable": 1,
				"fields": [
					{"fieldname": "qty", "label": "qty", "fieldtype": "Float"},
					{"fieldname": "item_code", "label": "item_code", "fieldtype": "Data"},
				],
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("DocType", SOURCE):
		fields = [
			{
				"fieldname": k,
				"label": k,
				"fieldtype": t,
				**({"options": o} if o else {}),
				**({"permlevel": 1} if k == "restricted_amount" else {}),
			}
			for k, t, o in [
				("company", "Link", "Company"),
				("hospital", "Link", "HRP Hospital"),
				("organization_unit", "Link", "HRP Organization Unit"),
				("posting_date", "Date", None),
				("status", "Select", "Draft\nApproved"),
				("currency", "Link", "Currency"),
				("amount", "Currency", "currency"),
				("limit_amount", "Currency", "currency"),
				("restricted_amount", "Currency", "currency"),
				("comment", "Small Text", None),
				("items", "Table", CHILD),
			]
		]
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": SOURCE,
				"module": "Custom",
				"custom": 1,
				"autoname": "hash",
				"fields": fields,
				"permissions": [
					{"role": "System Manager", "read": 1, "write": 1, "create": 1},
					{"role": "System Manager", "permlevel": 1, "read": 1, "write": 1},
					{"role": "HRP User", "read": 1},
				],
			}
		).insert(ignore_permissions=True)
	frappe.local.db.commit()


def reset_rules() -> None:
	frappe.set_user("Administrator")
	# IntegrationTestCase rolls back at class end; restore metadata explicitly between methods.
	frappe.db.set_value("DocField", {"parent": SOURCE, "fieldname": "amount"}, {"permlevel": 0, "mask": 0})
	frappe.db.delete("DocPerm", {"parent": SOURCE, "role": "HRP User", "permlevel": 1})
	frappe.clear_cache(doctype=SOURCE)
	frappe.db.delete(LOG)
	frappe.db.delete(RULE)
	frappe.db.delete(CHILD)
	frappe.db.delete(SOURCE)
	frappe.db.delete("HRP Access Scope Member", {"parent": "COD029-SCOPE"})
	frappe.db.delete("HRP Access Scope", {"code": "COD029-SCOPE"})
	frappe.db.delete(
		"HRP Service Idempotency",
		{
			"service_name": (
				"in",
				[UpsertBusinessRule.definition.name, RecordBusinessRuleSnapshot.definition.name],
			)
		},
	)


def policy(**overrides):
	return rule_payload(
		{
			"code": "COD029-RULE",
			"display_name": "申请金额约束",
			"target_doctype": SOURCE,
			"target_action": "validate",
			"company": TEST_COMPANY,
			"hospital": TEST_HOSPITAL,
			"organization_unit": TEST_UNIT,
			"valid_from": "2026-01-01",
			"severity": "Block",
			"assertion_json": {"field": "amount", "op": "lte", "other_field": "limit_amount"},
			**overrides,
		}
	)


def upsert(payload=None, *, revision=0, label="create"):
	return UpsertBusinessRule().execute(
		{"policy": payload or policy(), "expected_revision": revision},
		idempotency_key=deduplication_identifier(label),
	)


def source(**overrides):
	return frappe.get_doc(
		{
			"doctype": SOURCE,
			"company": TEST_COMPANY,
			"hospital": TEST_HOSPITAL,
			"organization_unit": TEST_UNIT,
			"posting_date": "2026-06-30",
			"status": "Draft",
			"currency": "CNY",
			"amount": 10,
			"limit_amount": 20,
			"restricted_amount": 7,
			"comment": "SENSITIVE-SOURCE-MARKER-COD029",
			"items": [{"qty": 1, "item_code": "ITEM"}],
			**overrides,
		}
	).insert(ignore_permissions=True)


def evaluate(doc, action="validate") -> dict[str, Any]:
	return (
		EvaluateBusinessRules().execute({"doctype": doc.doctype, "name": doc.name, "action": action}).result
	)


def snapshot_command(doc, decision):
	return {
		"doctype": doc.doctype,
		"name": doc.name,
		"action": decision["target_action"],
		"expected_source_modified": decision["source_modified"],
		"expected_decision_digest": decision["decision_digest"],
	}


def grant_source_read():
	frappe.get_doc(
		{
			"doctype": "HRP Access Scope",
			"code": "COD029-SCOPE",
			"display_name": "业务规则测试范围",
			"company": TEST_COMPANY,
			"hospital": TEST_HOSPITAL,
			"organization_unit": TEST_UNIT,
			"enabled": 1,
			"members": [
				{"user": TEST_USER, "target_doctype": dt, "allow_read": 1, "enabled": 1}
				for dt in (SOURCE, LOG)
			],
		}
	).insert(ignore_permissions=True)


class TestBusinessRules(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_rule_fixtures()

	def setUp(self):
		super().setUp()
		reset_rules()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()
		frappe.clear_cache(doctype=SOURCE)

	def test_schema_idempotent_migration_and_direct_write_guard(self):
		self.assertEqual(ensure_business_rule_governance(), ensure_business_rule_governance())
		self.assertFalse(frappe.get_meta(LOG).track_changes)
		self.assertFalse(any(p.write or p.create for p in frappe.get_meta(LOG).permissions))
		with self.assertRaises(IoneApplicationError):
			frappe.get_doc({"doctype": RULE, **policy()}).insert(ignore_permissions=True)

	def test_upsert_replay_revision_and_immutable_identity(self):
		self.assertEqual(upsert().result["revision"], 1)
		self.assertTrue(upsert().idempotency_replayed)
		self.assertEqual(upsert(policy(priority=3), revision=1, label="revise").result["revision"], 2)
		with self.assertRaises(IoneApplicationError):
			upsert(policy(priority=5), revision=1, label="stale")
		with self.assertRaises(IoneApplicationError):
			upsert(policy(target_action="submit"), revision=2, label="identity")

	def test_live_query_never_replays_after_source_or_policy_changes(self):
		upsert()
		doc = source()
		self.assertTrue(evaluate(doc)["allowed"])
		doc.amount = 30
		doc.save(ignore_permissions=True)
		self.assertFalse(evaluate(doc)["allowed"])
		upsert(policy(severity="Warn"), revision=1, label="warning")
		result = evaluate(doc)
		self.assertTrue(result["allowed"])
		self.assertEqual(result["outcome"], "Warned")
		self.assertEqual(frappe.db.count(LOG), 0)

	def test_when_false_is_not_an_authorization(self):
		upsert(policy(when_json={"field": "status", "op": "eq", "value": "Approved"}))
		result = evaluate(source())
		self.assertTrue(result["configured"])
		self.assertFalse(result["allowed"])
		self.assertEqual(result["rules"][0]["status"], "Skipped")

	def test_no_rules_denies_required_guard_and_keeps_optional_bridge_unconfigured(self):
		doc = source()
		self.assertFalse(evaluate(doc)["allowed"])
		enforce_business_rules(doc, "submit", required=False)
		with self.assertRaises(IoneApplicationError):
			enforce_business_rules(doc, "submit")

	def test_children_require_nonempty_and_all_rows_valid(self):
		upsert(
			policy(
				assertion_json={
					"table": "items",
					"quantifier": "all",
					"where": {"field": "qty", "op": "gt", "value": 0},
				}
			)
		)
		doc = source(items=[])
		self.assertFalse(evaluate(doc)["allowed"])
		doc.append("items", {"qty": 1})
		doc.append("items", {"qty": 0})
		doc.save(ignore_permissions=True)
		self.assertFalse(evaluate(doc)["allowed"])
		doc.items[1].qty = 2
		doc.save(ignore_permissions=True)
		self.assertTrue(evaluate(doc)["allowed"])

	def test_snapshot_is_append_only_and_contains_no_raw_source_values(self):
		upsert(policy(assertion_json={"field": "comment", "op": "required"}))
		doc = source()
		decision = evaluate(doc)
		cmd = snapshot_command(doc, decision)
		first = RecordBusinessRuleSnapshot().execute(cmd, idempotency_key=deduplication_identifier("record"))
		self.assertNotIn("allowed", first.result)
		self.assertTrue(
			RecordBusinessRuleSnapshot()
			.execute(cmd, idempotency_key=deduplication_identifier("record"))
			.idempotency_replayed
		)
		self.assertEqual(frappe.db.count(LOG), 1)
		log = frappe.get_doc(LOG, str(first.result["log"]))
		self.assertNotIn(doc.comment, json.dumps(log.as_dict(), default=str))
		log.outcome = "Blocked"
		with self.assertRaises(IoneApplicationError):
			log.save(ignore_permissions=True)
		with self.assertRaises(IoneApplicationError):
			frappe.delete_doc(LOG, log.name, ignore_permissions=True)

	def test_stale_snapshot_refused_and_old_evidence_does_not_become_live_decision(self):
		upsert()
		doc = source()
		decision = evaluate(doc)
		cmd = snapshot_command(doc, decision)
		first = RecordBusinessRuleSnapshot().execute(
			cmd, idempotency_key=deduplication_identifier("historical")
		)
		doc.amount = 100
		doc.save(ignore_permissions=True)
		with self.assertRaises(IoneApplicationError):
			RecordBusinessRuleSnapshot().execute(
				cmd, idempotency_key=deduplication_identifier("stale-source")
			)
		self.assertFalse(evaluate(doc)["allowed"])
		self.assertTrue(
			RecordBusinessRuleSnapshot()
			.execute(cmd, idempotency_key=deduplication_identifier("historical"))
			.idempotency_replayed
		)
		self.assertEqual(get_log(str(first.result["log"]))["recorded_outcome"], "Passed")
		self.assertTrue(get_log(str(first.result["log"]))["historical_evidence_only"])

	def test_policy_change_invalidates_snapshot_binding(self):
		upsert()
		doc = source()
		cmd = snapshot_command(doc, evaluate(doc))
		upsert(policy(severity="Warn"), revision=1, label="change-before-record")
		with self.assertRaises(IoneApplicationError):
			RecordBusinessRuleSnapshot().execute(
				cmd, idempotency_key=deduplication_identifier("stale-policy")
			)

	def test_scope_is_checked_before_returning_decisions_or_logs(self):
		upsert()
		doc = source()
		saved = RecordBusinessRuleSnapshot().execute(
			snapshot_command(doc, evaluate(doc)), idempotency_key=deduplication_identifier("scoped-record")
		)
		frappe.set_user(TEST_USER)
		with self.assertRaises(IoneApplicationError):
			evaluate(doc)
		with self.assertRaises(IoneApplicationError):
			get_log(str(saved.result["log"]))
		self.assertEqual(list_logs()["items"], [])
		frappe.set_user("Administrator")
		grant_source_read()
		frappe.set_user(TEST_USER)
		self.assertTrue(evaluate(doc)["allowed"])
		self.assertEqual(len(cast(list, list_logs()["items"])), 1)

	def test_source_field_permission_prevents_private_value_inference(self):
		upsert(policy(assertion_json={"field": "restricted_amount", "op": "gt", "value": 0}))
		doc = source()
		saved = RecordBusinessRuleSnapshot().execute(
			snapshot_command(doc, evaluate(doc)), idempotency_key=deduplication_identifier("private-record")
		)
		grant_source_read()
		frappe.set_user(TEST_USER)
		with self.assertRaises(IoneApplicationError):
			evaluate(doc)
		with self.assertRaises(IoneApplicationError):
			get_log(str(saved.result["log"]))
		self.assertEqual(list_logs()["items"], [])

	def test_metadata_drift_fails_closed_until_controlled_revision(self):
		upsert()
		doc = source()
		field = frappe.db.get_value("DocField", {"parent": SOURCE, "fieldname": "amount"}, "name")
		frappe.db.set_value("DocField", field, "permlevel", 1)
		frappe.clear_cache(doctype=SOURCE)
		with self.assertRaises(IoneApplicationError):
			evaluate(doc)
		upsert(revision=1, label="refresh-contract")
		self.assertTrue(evaluate(doc)["allowed"])
		self.assertEqual(frappe.db.get_value(RULE, "COD029-RULE", "revision"), 2)

	def test_masked_field_does_not_leak_through_decision(self):
		field = frappe.db.get_value("DocField", {"parent": SOURCE, "fieldname": "amount"}, "name")
		frappe.db.set_value("DocField", field, "mask", 1)
		frappe.clear_cache(doctype=SOURCE)
		upsert()
		doc = source()
		grant_source_read()
		frappe.set_user(TEST_USER)
		with self.assertRaises(IoneApplicationError):
			evaluate(doc)

	def test_owner_condition_on_higher_field_permission_is_enforced(self):
		frappe.get_doc(
			{
				"doctype": "DocPerm",
				"parent": SOURCE,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": "HRP User",
				"permlevel": 1,
				"read": 1,
				"if_owner": 1,
			}
		).db_insert()
		frappe.clear_cache(doctype=SOURCE)
		upsert(policy(assertion_json={"field": "restricted_amount", "op": "gt", "value": 0}))
		other = source()
		frappe.set_user(TEST_USER)
		owned = source()
		self.assertEqual(owned.owner, TEST_USER)
		frappe.set_user("Administrator")
		grant_source_read()
		frappe.set_user(TEST_USER)
		with self.assertRaises(IoneApplicationError):
			evaluate(other)
		self.assertTrue(evaluate(owned)["allowed"])

	def test_rule_count_fails_instead_of_evaluating_truncated_subset(self):
		for i in range(65):
			upsert(policy(code=f"COD029-LIMIT-{i}"), label=f"limit-{i}")
		doc = source()
		with self.assertRaises(IoneApplicationError):
			evaluate(doc)
		upsert(policy(code="COD029-LIMIT-0", enabled=0), revision=1, label="disable-limit")
		self.assertEqual(evaluate(doc)["rules_evaluated"], 64)


class TestBusinessRulesAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_rule_fixtures()

	def setUp(self):
		super().setUp()
		reset_rules()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def test_http_write_requires_key_and_live_evaluate_needs_no_key(self):
		missing = self.post(self.method(API + "upsert"), {"policy": policy()})
		self.assertEqual(missing.status_code, 400, missing.get_data(as_text=True))
		created = self.post(
			self.method(API + "upsert"),
			{"policy": policy()},
			headers={"Idempotency-Key": deduplication_identifier("http-upsert")},
		)
		self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
		doc = source()
		frappe.local.db.commit()
		response = self.post(
			self.method(API + "evaluate"), {"doctype": SOURCE, "name": doc.name, "action": "validate"}
		)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		decision = response.get_json()["message"]
		self.assertTrue(decision["allowed"])
		self.assertFalse(decision["idempotency_replayed"])
		args = {
			"doctype": SOURCE,
			"name": doc.name,
			"action": "validate",
			"expected_source_modified": decision["source_modified"],
			"expected_decision_digest": decision["decision_digest"],
		}
		for replay in (False, True):
			response = self.post(
				self.method(API + "record"),
				args,
				headers={"Idempotency-Key": deduplication_identifier("http-record")},
			)
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
			payload = response.get_json()["message"]
			self.assertTrue(payload["historical_evidence_only"])
			self.assertNotIn("allowed", payload)
			self.assertEqual(payload["idempotency_replayed"], replay)
		read = self.get(self.method(API + "execution_log"), {"log_name": payload["log"]})
		self.assertEqual(read.status_code, 200, read.get_data(as_text=True))

	def test_http_rejects_unknown_context_bad_pagination_and_missing_reference(self):
		response = self.post(
			self.method(API + "evaluate"),
			{"doctype": SOURCE, "name": "missing", "action": "validate", "company": TEST_COMPANY},
		)
		self.assertEqual(response.status_code, 400)
		self.assertEqual(self.get(self.method(API + "logs"), {"limit": 51}).status_code, 400)
		self.assertEqual(
			self.get(self.method(API + "execution_log"), {"log_name": "missing"}).status_code, 404
		)

	def test_http_guest_rejected_before_parsing_all_endpoints(self):
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		for name in ("upsert", "evaluate", "record"):
			self.assertEqual(self.post(self.method(API + name), {}).status_code, 401)
		for name in ("get", "execution_log", "logs"):
			self.assertEqual(self.get(self.method(API + name), {}).status_code, 401)

	def test_http_authenticated_user_without_scope_cannot_evaluate_or_configure(self):
		from frappe.auth import CookieManager, LoginManager
		from frappe.utils import set_request

		upsert()
		doc = source()
		frappe.local.db.commit()
		original_request = getattr(frappe.local, "request", None)
		set_request(path="/")
		try:
			frappe.local.cookie_manager = CookieManager()
			frappe.local.login_manager = LoginManager()
			frappe.local.login_manager.login_as(TEST_USER)
			sid = frappe.session.sid
		finally:
			frappe.local.request = original_request
			frappe.set_user("Administrator")
		self.TEST_CLIENT.set_cookie(key="sid", value=sid)
		response = self.post(
			self.method(API + "evaluate"), {"doctype": SOURCE, "name": doc.name, "action": "validate"}
		)
		self.assertEqual(response.status_code, 403, response.get_data(as_text=True))
		response = self.post(
			self.method(API + "upsert"),
			{"policy": policy()},
			headers={"Idempotency-Key": deduplication_identifier("http-denied")},
		)
		self.assertEqual(response.status_code, 403, response.get_data(as_text=True))
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)
		frappe.set_user("Administrator")
		upsert(
			policy(assertion_json={"field": "restricted_amount", "op": "gt", "value": 0}),
			revision=1,
			label="private-log-http",
		)
		RecordBusinessRuleSnapshot().execute(
			snapshot_command(doc, evaluate(doc)),
			idempotency_key=deduplication_identifier("private-log-http-record"),
		)
		grant_source_read()
		frappe.local.db.commit()
		self.TEST_CLIENT.set_cookie(key="sid", value=sid)
		response = self.get(self.method(API + "logs"), {"doctype": SOURCE, "name": doc.name})
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		self.assertEqual(response.get_json()["message"]["items"], [])
		self.assertNotIn("ione_error", response.get_json())
