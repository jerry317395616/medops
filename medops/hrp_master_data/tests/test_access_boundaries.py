from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from typing import Any, cast
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.api.v1.master_data_access import list_records
from medops.common.data_quality import build_data_quality_evaluate
from medops.common.domain_service import idempotency_record_name
from medops.common.error_catalog import IoneApplicationError
from medops.common.master_data import (
	MASTER_DATA_TARGET_POLICIES,
	build_master_data_request_review,
	build_master_data_request_submit,
)
from medops.common.numbering import build_number_allocation, build_numbering_scheme_upsert
from medops.common.organization import build_hospital_upsert
from medops.hrp_foundation.services.numbering import (
	allocate_number,
	get_number_reservation,
	upsert_numbering_scheme,
)
from medops.hrp_master_data.services.data_quality import (
	UpsertDataQualityRuleService,
	evaluate_data_quality,
	get_data_quality_issue,
	run_data_quality_rule_batch,
	run_data_quality_rules,
	upsert_data_quality_rule,
)
from medops.hrp_master_data.services.master_data import (
	get_master_data_request,
	review_master_data_request,
	save_master_data_request,
	submit_master_data_request,
)
from medops.hrp_master_data.tests.test_data_quality import (
	TEST_COMPANY,
	TEST_HOSPITAL,
	TEST_ITEM,
	create_data_quality_context,
	ensure_data_quality_fixtures,
	reset_data_quality_state,
	rule_command,
)
from medops.hrp_master_data.tests.test_master_data import TEST_COMPANY as OTHER_COMPANY
from medops.hrp_master_data.tests.test_master_data import TEST_HOSPITAL as OTHER_HOSPITAL
from medops.hrp_master_data.tests.test_master_data import TEST_ITEM as OTHER_ITEM
from medops.hrp_master_data.tests.test_master_data import (
	create_context,
	create_domain,
	ensure_master_data_fixtures,
	request_command,
	reset_master_data_state,
)
from medops.hrp_organization.services.organization import get_organization_hierarchy, upsert_hospital
from medops.setup.master_data_access import ensure_master_data_access_contracts

ACTOR = "cod201-actor@example.com"
PEER = "cod201-peer@example.com"
METHOD = "medops.api.v1.master_data_access.list_records"


def key(label: str) -> str:
	return "COD201-" + label


def ensure_access_fixtures() -> None:
	ensure_master_data_fixtures()
	ensure_data_quality_fixtures()
	for user in (ACTOR, PEER):
		if not frappe.db.exists("User", user):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user,
					"first_name": "Scope Test",
					"enabled": 1,
					"user_type": "System User",
					"send_welcome_email": 0,
					"roles": [{"role": "HRP Data Steward"}, {"role": "HRP User"}],
				}
			).insert(ignore_permissions=True)
	frappe.local.db.commit()


def reset_access_state() -> tuple[dict[str, Any], str]:
	frappe.set_user("Administrator")
	reset_data_quality_state()
	reset_master_data_state()
	frappe.db.delete("HRP Access Scope Member", {"parent": ("like", "COD201-%")})
	frappe.db.delete("HRP Access Scope", {"code": ("like", "COD201-%")})
	frappe.db.delete("HRP Number Reservation", {"scheme_code": "COD201-NUM"})
	frappe.db.delete("HRP Numbering Scheme", {"code": "COD201-NUM"})
	for user in (ACTOR, PEER):
		frappe.db.delete("DocShare", {"user": user})
		frappe.db.delete("Has Role", {"parent": user, "role": "Stock User"})
		frappe.clear_cache(user=user)
	create_domain(suffix="security")
	version, unit = create_context(suffix="security")
	create_data_quality_context(suffix="security")
	return cast(dict[str, Any], version), unit


def grant_scope(
	user=ACTOR, *, company=TEST_COMPANY, hospital=TEST_HOSPITAL, organization_unit=None, suffix="main"
):
	doc = frappe.get_doc(
		{
			"doctype": "HRP Access Scope",
			"code": key(suffix + "-" + user.split("@")[0]),
			"display_name": "访问边界测试",
			"company": company,
			"hospital": hospital,
			"organization_unit": organization_unit,
			"enabled": 1,
			"members": [
				{
					"user": user,
					"enabled": 1,
					"allow_read": 1,
					"allow_create": 1,
					"allow_write": 1,
					"allow_submit": 1,
					"allow_export": 1,
				}
			],
		}
	).insert(ignore_permissions=True)
	return doc


def share_item(user=ACTOR, target=TEST_ITEM):
	frappe.share.add("Item", target, user=user, read=1, write=0, share=0, notify=0)


def create_issue() -> tuple[dict[str, Any], dict[str, Any]]:
	created = upsert_data_quality_rule(rule_command(code="COD201-RULE"), idempotency_key=key("rule"))
	result = evaluate_data_quality(
		build_data_quality_evaluate(
			rule_name=created["name"],
			target_name=TEST_ITEM,
			effective_on="2026-08-08",
			expected_rule_revision=1,
		),
		idempotency_key=key("evaluate"),
	)
	assert result["issue"]
	return cast(dict[str, Any], created), cast(dict[str, Any], result["issue"])


@contextmanager
def private_field(doctype: str, fieldname: str, *, commit=False):
	field = frappe.db.get_value("DocField", {"parent": doctype, "fieldname": fieldname}, "name")
	before = frappe.db.get_value("DocField", field, ["permlevel", "mask"], as_dict=True)
	frappe.db.set_value("DocField", field, {"permlevel": 1, "mask": 1})
	frappe.clear_cache(doctype=doctype)
	if commit:
		frappe.local.db.commit()
	try:
		yield
	finally:
		frappe.db.set_value(
			"DocField", field, {"permlevel": before.permlevel, "mask": before.get("mask") or 0}
		)
		frappe.clear_cache(doctype=doctype)
		if commit:
			frappe.local.db.commit()


def private_item_name(*, commit=False):
	return private_field("Item", "item_name", commit=commit)


class TestMasterDataAccess(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_access_fixtures()

	def setUp(self):
		super().setUp()
		self.version, self.unit = reset_access_state()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_no_scope_role_cannot_repeat_the_verified_read_or_write_bypass(self):
		created, issue = create_issue()
		frappe.set_user(ACTOR)
		self.assertEqual(list_records(entity="rules")["items"], [])
		self.assertEqual(frappe.get_list("HRP Data Quality Rule", pluck="name"), [])
		self.assertFalse(
			frappe.has_permission(
				"HRP Data Quality Issue",
				ptype="read",
				doc=frappe.get_doc("HRP Data Quality Issue", issue["name"]),
			)
		)
		with self.assertRaises(IoneApplicationError):
			get_data_quality_issue(issue_name=issue["name"])
		with self.assertRaises(IoneApplicationError):
			upsert_data_quality_rule(rule_command(code="COD201-DENIED"), idempotency_key=key("unscoped"))
		self.assertFalse(frappe.db.exists("HRP Data Quality Rule", "COD201-DENIED"))
		self.assertTrue(frappe.db.exists("HRP Data Quality Rule", created["name"]))

	def test_scope_does_not_grant_native_target_read_but_explicit_share_restores_access(self):
		_, issue = create_issue()
		grant_scope()
		frappe.set_user(ACTOR)
		with self.assertRaises(IoneApplicationError):
			get_data_quality_issue(issue_name=issue["name"])
		self.assertEqual(list_records(entity="issues")["items"], [])
		frappe.set_user("Administrator")
		share_item()
		frappe.set_user(ACTOR)
		self.assertEqual(get_data_quality_issue(issue_name=issue["name"])["issue_status"], "Open")
		self.assertEqual(len(cast(list, list_records(entity="issues")["items"])), 1)
		self.assertEqual(frappe.get_list("HRP Data Quality Issue", pluck="name"), [])

	def test_cross_hospital_scope_is_not_widened_by_the_same_business_role(self):
		created, _ = create_issue()
		other = upsert_data_quality_rule(
			replace(
				rule_command(code="COD201-OTHER"),
				company=OTHER_COMPANY,
				hospital=OTHER_HOSPITAL,
				organization_unit=self.unit,
			),
			idempotency_key=key("other-rule"),
		)
		grant_scope()
		frappe.set_user(ACTOR)
		names = {r["name"] for r in cast(list[dict], list_records(entity="rules")["items"])}
		self.assertIn(created["name"], names)
		self.assertNotIn(other["name"], names)
		with self.assertRaises(IoneApplicationError):
			upsert_data_quality_rule(
				replace(
					rule_command(code="COD201-OTHER-WRITE"),
					company=OTHER_COMPANY,
					hospital=OTHER_HOSPITAL,
					organization_unit=self.unit,
				),
				idempotency_key=key("cross-hospital-write"),
			)

	def test_rule_field_changes_cannot_release_private_historical_verdicts(self):
		created, issue = create_issue()
		grant_scope()
		share_item()
		with private_item_name():
			upsert_data_quality_rule(
				rule_command(
					rule_name=created["name"],
					code="COD201-RULE",
					target_field="stock_uom",
					rule_type="Required",
					parameters={},
					expected_revision=1,
				),
				idempotency_key=key("change-field"),
			)
			frappe.set_user(ACTOR)
			with self.assertRaises(IoneApplicationError):
				get_data_quality_issue(issue_name=issue["name"])
			frappe.set_user("Administrator")
			evaluate_data_quality(
				build_data_quality_evaluate(
					rule_name=created["name"],
					target_name=TEST_ITEM,
					effective_on="2026-08-08",
					expected_rule_revision=2,
				),
				idempotency_key=key("new-field-evaluation"),
			)
			self.assertEqual(
				set(
					json.loads(
						frappe.db.get_value("HRP Data Quality Issue", issue["name"], "evaluation_read_fields")
					)
				),
				{"item_name", "stock_uom"},
			)
			frappe.set_user(ACTOR)
			with self.assertRaises(IoneApplicationError):
				get_data_quality_issue(issue_name=issue["name"])
		frappe.set_user(ACTOR)
		self.assertEqual(get_data_quality_issue(issue_name=issue["name"])["issue_status"], "Resolved")

	def test_legacy_migration_is_conservative_repeatable_and_cannot_shrink(self):
		_, issue = create_issue()
		before = frappe.get_doc("HRP Data Quality Issue", issue["name"])
		business = {
			k: before.get(k)
			for k in (
				"issue_status",
				"first_detected_at",
				"last_evaluated_at",
				"occurrence_count",
				"observed_value_digest",
			)
		}
		frappe.db.set_value("HRP Data Quality Issue", before.name, "evaluation_read_fields", None)
		self.assertEqual(ensure_master_data_access_contracts()["updated"], 1)
		self.assertEqual(ensure_master_data_access_contracts()["updated"], 0)
		after = frappe.get_doc("HRP Data Quality Issue", before.name)
		self.assertEqual(
			set(json.loads(after.evaluation_read_fields)),
			set(MASTER_DATA_TARGET_POLICIES["Item"].fields_by_name),
		)
		self.assertEqual({k: after.get(k) for k in business}, business)
		self.assertEqual(after.revision, before.revision + 1)
		after.evaluation_read_fields = '["item_name"]'
		after.flags.data_quality_service_write = True
		after.flags.locked_revision = after.revision
		with self.assertRaises(IoneApplicationError):
			after.save(ignore_permissions=True)

	def test_completed_idempotency_stays_with_its_original_actor_and_live_scope(self):
		scope = grant_scope()
		grant_scope(PEER)
		frappe.set_user(ACTOR)
		command = rule_command(code="COD201-ACTOR-KEY")
		first = upsert_data_quality_rule(command, idempotency_key=key("same-actor"))
		self.assertTrue(
			upsert_data_quality_rule(command, idempotency_key=key("same-actor"))["idempotency_replayed"]
		)
		frappe.set_user(PEER)
		with self.assertRaises(IoneApplicationError) as raised:
			upsert_data_quality_rule(command, idempotency_key=key("same-actor"))
		self.assertEqual(raised.exception.code, "IONE-CORE-0007")
		frappe.set_user("Administrator")
		stored = frappe.get_doc(
			"HRP Service Idempotency",
			idempotency_record_name(UpsertDataQualityRuleService.definition.name, key("same-actor")),
		)
		self.assertEqual(stored.owner, ACTOR)
		self.assertEqual(frappe.db.count("HRP Data Quality Rule", {"name": first["name"]}), 1)
		frappe.db.set_value("HRP Access Scope", scope.name, "enabled", 0)
		frappe.set_user(ACTOR)
		with self.assertRaises(IoneApplicationError) as forbidden:
			upsert_data_quality_rule(command, idempotency_key=key("same-actor"))
		self.assertEqual(forbidden.exception.code, "IONE-CORE-0002")

	def test_authorized_request_submit_and_review_replay_preserve_dual_control(self):
		grant_scope(company=OTHER_COMPANY, hospital=OTHER_HOSPITAL)
		grant_scope(PEER, company=OTHER_COMPANY, hospital=OTHER_HOSPITAL)
		share_item(target=OTHER_ITEM)
		share_item(PEER, OTHER_ITEM)
		frappe.set_user(ACTOR)
		draft = save_master_data_request(request_command(self.unit), idempotency_key=key("draft"))
		submitted = submit_master_data_request(
			build_master_data_request_submit(request_name=draft["name"], expected_revision=draft["revision"]),
			idempotency_key=key("submit"),
		)
		with self.assertRaises(IoneApplicationError):
			review_master_data_request(
				build_master_data_request_review(
					request_name=draft["name"],
					expected_revision=submitted["revision"],
					decision="Approve",
					reason="自审必须失败",
				),
				idempotency_key=key("self-review"),
			)
		frappe.set_user(PEER)
		command = build_master_data_request_review(
			request_name=draft["name"],
			expected_revision=submitted["revision"],
			decision="Approve",
			reason="已复核",
		)
		approved = review_master_data_request(command, idempotency_key=key("review"))
		self.assertEqual(approved["request_status"], "Approved")
		self.assertTrue(
			review_master_data_request(command, idempotency_key=key("review"))["idempotency_replayed"]
		)
		self.assertEqual(get_master_data_request(request_name=draft["name"])["request_status"], "Approved")
		self.assertEqual(frappe.db.get_value("Item", OTHER_ITEM, "item_name"), "COD-020原始物料")

	def test_hierarchy_and_numbering_cannot_escape_hospital_scope(self):
		frappe.set_user(ACTOR)
		with self.assertRaises(IoneApplicationError):
			get_organization_hierarchy(organization_version=self.version["name"])
		with self.assertRaises(IoneApplicationError):
			upsert_hospital(
				build_hospital_upsert(
					code="COD201-NO-HOSPITAL",
					company=TEST_COMPANY,
					display_name="拒绝的医院",
					expected_revision=0,
				),
				idempotency_key=key("hospital"),
			)
		frappe.set_user("Administrator")
		upsert_numbering_scheme(
			build_numbering_scheme_upsert(
				code="COD201-NUM",
				display_name="编号边界",
				company=TEST_COMPANY,
				hospital=TEST_HOSPITAL,
				template="SEC-{YYYY}-{SEQ}",
				reset_policy="Yearly",
				sequence_digits=4,
			),
			idempotency_key=key("numbering-scheme"),
		)
		command = build_number_allocation(scheme="COD201-NUM", dimensions={}, business_date="2026-10-25")
		frappe.set_user(ACTOR)
		with self.assertRaises(IoneApplicationError):
			allocate_number(command, idempotency_key=key("number"))
		self.assertEqual(frappe.db.count("HRP Number Reservation", {"scheme_code": "COD201-NUM"}), 0)
		frappe.set_user("Administrator")
		grant_scope(
			company=OTHER_COMPANY, hospital=OTHER_HOSPITAL, organization_unit=self.unit, suffix="leaf"
		)
		share_item(target=OTHER_ITEM)
		root = frappe.db.get_value(
			"HRP Organization Unit",
			{"organization_version": self.version["name"], "code": OTHER_HOSPITAL},
			"name",
		)
		frappe.set_user(ACTOR)
		draft = save_master_data_request(request_command(self.unit), idempotency_key=key("leaf-request"))
		self.assertEqual(get_master_data_request(request_name=draft["name"])["name"], draft["name"])
		with self.assertRaises(IoneApplicationError) as outside:
			save_master_data_request(request_command(root), idempotency_key=key("outside-leaf"))
		self.assertEqual(outside.exception.code, "IONE-CORE-0002")
		with self.assertRaises(IoneApplicationError):
			get_organization_hierarchy(organization_version=self.version["name"])
		frappe.set_user("Administrator")
		grant_scope()
		grant_scope(PEER)
		grant_scope(company=OTHER_COMPANY, hospital=OTHER_HOSPITAL, suffix="org")
		frappe.set_user(ACTOR)
		self.assertEqual(
			len(cast(list, get_organization_hierarchy(organization_version=self.version["name"])["nodes"])),
			self.version["node_count"],
		)
		allocated = allocate_number(command, idempotency_key=key("number"))
		self.assertEqual(
			get_number_reservation(str(allocated["reservation_token"]))["number"], allocated["number"]
		)
		frappe.set_user(PEER)
		with self.assertRaises(IoneApplicationError):
			get_number_reservation(str(allocated["reservation_token"]))

	def test_queued_quality_work_rechecks_revoked_scope(self):
		created, _ = create_issue()
		scope = grant_scope()
		frappe.get_doc("User", ACTOR).add_roles("Stock User")
		frappe.set_user(ACTOR)
		with patch("medops.hrp_master_data.services.data_quality.enqueue_with_audit") as enqueue:
			self.assertEqual(run_data_quality_rules()["scheduled_rule_count"], 1)
			self.assertEqual(enqueue.call_count, 1)
		frappe.set_user("Administrator")
		frappe.db.set_value("HRP Access Scope", scope.name, "enabled", 0)
		frappe.set_user(ACTOR)
		with self.assertRaises(IoneApplicationError):
			run_data_quality_rule_batch(
				rule_name=created["name"], effective_on="2026-08-08", expected_rule_revision=1
			)

	def test_private_result_fields_block_create_and_replay_inside_the_transaction(self):
		grant_scope()
		frappe.set_user(ACTOR)
		command = rule_command(code="COD201-PRIVATE-RESULT")
		with private_field("HRP Data Quality Rule", "display_name"):
			with self.assertRaises(IoneApplicationError) as denied:
				upsert_data_quality_rule(command, idempotency_key=key("private-result"))
			self.assertEqual(denied.exception.code, "IONE-CORE-0002")
			self.assertFalse(frappe.db.exists("HRP Data Quality Rule", command.code))
			self.assertFalse(
				frappe.db.exists(
					"HRP Service Idempotency",
					idempotency_record_name(
						UpsertDataQualityRuleService.definition.name, key("private-result")
					),
				)
			)
		upsert_data_quality_rule(command, idempotency_key=key("private-result"))
		with private_field("HRP Data Quality Rule", "display_name"):
			with self.assertRaises(IoneApplicationError) as denied:
				upsert_data_quality_rule(command, idempotency_key=key("private-result"))
			self.assertEqual(denied.exception.code, "IONE-CORE-0002")
		self.assertTrue(
			upsert_data_quality_rule(command, idempotency_key=key("private-result"))["idempotency_replayed"]
		)
		self.assertEqual(frappe.db.count("HRP Data Quality Rule", {"name": command.code}), 1)

	def test_scheduled_keys_preserve_legacy_retries_and_isolate_new_actors(self):
		created = upsert_data_quality_rule(
			rule_command(code="COD201-SCHEDULE-KEY"), idempotency_key=key("schedule-rule")
		)
		name = str(created["name"])
		digest = sha256(f"{name}\0{1}\0{TEST_ITEM}\0{2026}-08-08".encode()).hexdigest()
		legacy = "DQ-2026-08-08-" + digest[:32]
		result = evaluate_data_quality(
			build_data_quality_evaluate(
				rule_name=name, target_name=TEST_ITEM, effective_on="2026-08-08", expected_rule_revision=1
			),
			idempotency_key=legacy,
		)
		issue = cast(dict[str, Any], result["issue"])
		for user in (ACTOR, PEER):
			grant_scope(user)
			frappe.get_doc("User", user).add_roles("Stock User")
		from medops.hrp_foundation.services.reference_links import register_references
		from medops.hrp_master_data.tests.test_data_quality import data_quality_domain

		register_references(
			{"company": TEST_COMPANY, "hospital": TEST_HOSPITAL, "master_data_domain": data_quality_domain()},
			[{"target_name": TEST_ITEM, "valid_from": "2026-01-01"}],
			idempotency_key=key("schedule-population"),
		)
		for user, expected_count in (("Administrator", 1), (ACTOR, 2), (ACTOR, 2), (PEER, 3), (PEER, 3)):
			frappe.set_user(user)
			batch = run_data_quality_rule_batch(
				rule_name=name, effective_on="2026-08-08", expected_rule_revision=1
			)
			self.assertEqual(batch, {"processed": 1, "failed": 0, "has_more": False})
			self.assertEqual(
				frappe.db.get_value("HRP Data Quality Issue", issue["name"], "occurrence_count"),
				expected_count,
			)


class TestMasterDataAccessAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_access_fixtures()

	def setUp(self):
		super().setUp()
		reset_access_state()
		self.created, self.issue = create_issue()
		frappe.local.db.commit()

	def login_actor(self):
		from frappe.auth import CookieManager, LoginManager
		from frappe.utils import set_request

		old = getattr(frappe.local, "request", None)
		set_request(path="/")
		try:
			frappe.local.cookie_manager = CookieManager()
			frappe.local.login_manager = LoginManager()
			frappe.local.login_manager.login_as(ACTOR)
			sid = frappe.session.sid
		finally:
			frappe.local.request = old
			frappe.set_user("Administrator")
		self.TEST_CLIENT.set_cookie(key="sid", value=sid)

	def test_http_guest_validation_scope_and_generic_list_boundaries(self):
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		self.assertEqual(self.get(self.method(METHOD), {"entity": "unknown"}).status_code, 401)
		self.login_actor()
		response = self.get(self.method(METHOD), {"entity": "issues"})
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		self.assertEqual(response.get_json()["message"]["items"], [])
		self.assertEqual(
			self.get(self.method(METHOD), {"entity": "issues", "user": "Administrator"}).status_code, 400
		)
		self.assertEqual(self.get(self.method(METHOD), {"entity": "issues", "limit": 51}).status_code, 400)
		frappe.set_user("Administrator")
		grant_scope()
		share_item()
		frappe.local.db.commit()
		response = self.get(self.method(METHOD), {"entity": "issues"})
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		self.assertEqual(len(response.get_json()["message"]["items"]), 1)
		self.assertNotIn("ione_error", response.get_json())
		generic = self.get(
			self.method("frappe.client.get_list"),
			{"doctype": "HRP Data Quality Issue", "fields": json.dumps(["name", "issue_status"])},
		)
		self.assertEqual(generic.status_code, 200, generic.get_data(as_text=True))
		self.assertEqual(generic.get_json()["message"], [])

	def test_http_field_denial_does_not_pollute_filtered_success(self):
		grant_scope()
		share_item()
		frappe.local.db.commit()
		self.login_actor()
		with private_item_name(commit=True):
			response = self.get(self.method(METHOD), {"entity": "issues"})
			self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
			self.assertEqual(response.get_json()["message"]["items"], [])
			self.assertNotIn("ione_error", response.get_json())
			denied = self.get(
				self.method("medops.api.v1.master_data.get_data_quality_issue"),
				{"issue_name": self.issue["name"]},
			)
			self.assertEqual(denied.status_code, 403)
