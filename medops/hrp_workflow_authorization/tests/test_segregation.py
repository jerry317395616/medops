from __future__ import annotations

from typing import cast
from unittest.mock import patch

import frappe
from frappe.boot import build_default_workspace_map, get_sidebar_items
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.error_catalog import IoneApplicationError
from medops.common.segregation import (
	build_segregation_evaluation,
	build_segregation_rule_upsert,
)
from medops.hrp_workflow_authorization.permissions import (
	can_read_segregation_rule,
	segregation_rule_query,
)
from medops.hrp_workflow_authorization.services.segregation import (
	UpsertSegregationRuleService,
	ValidateSegregationService,
	get_segregation_rule,
	upsert_segregation_rule,
	validate_segregation,
)
from medops.hrp_workflow_authorization.tests.test_approval_matrix import (
	TEST_COMPANY,
	TEST_HOSPITAL,
	TEST_PLAIN_USER,
	TEST_ROOT,
	TEST_UNIT,
	TEST_USER,
	TEST_VERSION,
	create_access_scope,
	ensure_approval_fixtures,
)
from medops.setup.segregation import ensure_segregation_governance
from medops.setup.workspaces import sync_owned_workspaces

VALIDATE_METHOD = "medops.api.v1.core.sod.validate"
UPSERT_METHOD = "medops.api.v1.core.sod.upsert_rule"
GET_METHOD = "medops.api.v1.core.sod.get_rule"
TEST_RULE = "COD027-ORG-MAKER-CHECKER"


def reset_segregation_state() -> None:
	frappe.set_user("Administrator")
	frappe.db.delete("HRP Segregation Rule", {"code": ("like", "COD027-%")})
	frappe.db.delete("HRP Access Scope Member", {"parent": ("like", "COD025-%")})
	frappe.db.delete("HRP Access Scope", {"code": ("like", "COD025-%")})
	frappe.db.delete(
		"HRP Service Idempotency",
		{
			"service_name": (
				"in",
				[
					UpsertSegregationRuleService.definition.name,
					ValidateSegregationService.definition.name,
				],
			)
		},
	)


def rule_command(
	*,
	code: str = TEST_RULE,
	actor_fields: object = ("owner",),
	conflicting_roles: object = (),
	organization_unit: str | None = TEST_UNIT,
	include_descendants: bool = False,
	expected_revision: int = 0,
	rule_name: str | None = None,
	display_name: str = "组织单元制单审批分离",
):
	return build_segregation_rule_upsert(
		rule_name=rule_name,
		expected_revision=expected_revision,
		code=code,
		display_name=display_name,
		target_doctype="HRP Organization Unit",
		target_action="approve",
		company=TEST_COMPANY,
		hospital=TEST_HOSPITAL,
		organization_unit=organization_unit,
		include_descendants=include_descendants,
		actor_fields=actor_fields,
		conflicting_roles=conflicting_roles,
		enabled=True,
		valid_from="2026-01-01",
	)


def evaluation_command(user: str = "Administrator"):
	return build_segregation_evaluation(
		user=user,
		action="approve",
		doctype="HRP Organization Unit",
		docname=TEST_UNIT,
	)


class TestSegregation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_approval_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_segregation_state()

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	def test_metadata_indexes_permissions_workspace_and_direct_write_guard(self) -> None:
		meta = frappe.get_meta("HRP Segregation Rule")
		self.assertEqual(meta.get_field("target_action").label, "受控动作")
		self.assertSetEqual(
			{permission.role for permission in meta.permissions},
			{"System Manager", "HRP System Manager", "HRP Auditor"},
		)
		self.assertEqual(ensure_segregation_governance()["schema_version"], 1)
		self.assertEqual(ensure_segregation_governance()["schema_version"], 1)
		indexes = {
			str(row.Key_name)
			for row in frappe.db.sql("SHOW INDEX FROM `tabHRP Segregation Rule`", as_dict=True)
		}
		self.assertTrue(
			{
				"idx_hrp_segregation_rule_resolution",
				"idx_hrp_segregation_rule_organization",
			}.issubset(indexes)
		)
		self.assertEqual(sync_owned_workspaces()["schema_version"], 1)
		workspace = frappe.get_doc("Workspace", "HRP Authorization")
		self.assertIn("HRP Segregation Rule", {shortcut.link_to for shortcut in workspace.shortcuts})
		workspace_map = build_default_workspace_map(get_sidebar_items())
		self.assertEqual(workspace_map["HRP Segregation Rule"], "HRP Authorization")
		unsafe = frappe.get_doc(
			{
				"doctype": "HRP Segregation Rule",
				"code": "COD027-DIRECT",
				"display_name": "禁止直接写入",
				"target_doctype": "HRP Organization Unit",
				"target_action": "approve",
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"organization_unit": TEST_UNIT,
				"actor_fields_json": '["owner"]',
				"valid_from": "2026-01-01",
			}
		)
		with self.assertRaises(IoneApplicationError) as raised:
			unsafe.insert(ignore_permissions=True)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")

	def test_upsert_replay_revision_get_and_permission_filter(self) -> None:
		created = upsert_segregation_rule(rule_command(), idempotency_key="COD-027-create-rule")
		replayed = upsert_segregation_rule(rule_command(), idempotency_key="COD-027-create-rule")
		self.assertEqual(created["name"], TEST_RULE)
		self.assertTrue(replayed["idempotency_replayed"])
		updated = upsert_segregation_rule(
			rule_command(
				rule_name=TEST_RULE,
				expected_revision=1,
				display_name="组织单元制单审批分离修订",
			),
			idempotency_key="COD-027-update-rule",
		)
		self.assertEqual(updated["revision"], 2)
		with self.assertRaises(IoneApplicationError) as stale:
			upsert_segregation_rule(
				rule_command(rule_name=TEST_RULE, expected_revision=1),
				idempotency_key="COD-027-stale-rule",
			)
		self.assertEqual(stale.exception.code, "IONE-CORE-0005")
		doc = frappe.get_doc("HRP Segregation Rule", TEST_RULE)
		self.assertEqual(segregation_rule_query(TEST_PLAIN_USER), "1=0")
		self.assertTrue(can_read_segregation_rule(doc, "Administrator", "write"))
		self.assertFalse(can_read_segregation_rule(doc, TEST_PLAIN_USER, "read"))
		self.assertEqual(get_segregation_rule(TEST_RULE)["revision"], 2)

	def test_validation_blocks_actor_and_role_conflicts_without_identity_leakage(self) -> None:
		upsert_segregation_rule(
			rule_command(),
			idempotency_key="COD-027-conflict-rule",
		)
		original_owner = frappe.db.get_value("HRP Organization Unit", TEST_UNIT, "owner")
		try:
			blocked = validate_segregation(evaluation_command())
			self.assertFalse(blocked["allowed"])
			conflicts = cast(list[dict[str, object]], blocked["conflicts"])
			self.assertEqual(conflicts[0]["reason"], "ACTOR_REUSE")
			self.assertNotIn("Administrator", str(blocked))
			self.assertFalse(blocked["idempotency_replayed"])

			frappe.db.set_value(
				"HRP Organization Unit",
				TEST_UNIT,
				"owner",
				TEST_USER,
				update_modified=False,
			)
			frappe.clear_document_cache("HRP Organization Unit", TEST_UNIT)
			allowed = validate_segregation(evaluation_command())
			self.assertTrue(allowed["allowed"])
			self.assertFalse(allowed["idempotency_replayed"])
			self.assertNotEqual(blocked["decision_digest"], allowed["decision_digest"])

			updated = upsert_segregation_rule(
				rule_command(
					rule_name=TEST_RULE,
					expected_revision=1,
					display_name="组织单元制单审批分离修订",
				),
				idempotency_key="COD-027-live-policy-revision",
			)
			self.assertEqual(updated["revision"], 2)
			revised = validate_segregation(evaluation_command())
			self.assertTrue(revised["allowed"])
			self.assertFalse(revised["idempotency_replayed"])
			self.assertNotEqual(allowed["decision_digest"], revised["decision_digest"])
			self.assertEqual(
				frappe.db.count(
					"HRP Service Idempotency",
					{"service_name": ValidateSegregationService.definition.name},
				),
				0,
			)
		finally:
			frappe.db.set_value(
				"HRP Organization Unit",
				TEST_UNIT,
				"owner",
				original_owner,
				update_modified=False,
			)
			frappe.clear_document_cache("HRP Organization Unit", TEST_UNIT)

	def test_validation_is_default_deny_and_allows_nonconflicting_scoped_user(self) -> None:
		missing = validate_segregation(evaluation_command())
		self.assertFalse(missing["allowed"])
		conflicts = cast(list[dict[str, object]], missing["conflicts"])
		self.assertEqual(conflicts[0]["code"], "NO_APPLICABLE_RULE")
		create_access_scope(user=TEST_USER)
		upsert_segregation_rule(rule_command(), idempotency_key="COD-027-allow-rule")
		with patch(
			"medops.hrp_workflow_authorization.services.segregation.frappe.has_permission",
			return_value=True,
		):
			allowed = validate_segregation(evaluation_command(TEST_USER))
		self.assertTrue(allowed["allowed"])
		self.assertEqual(allowed["conflicts"], [])

	def test_validation_rejects_invalid_user_scope_and_runtime_reference_drift(self) -> None:
		with self.assertRaises(IoneApplicationError) as invalid_field:
			upsert_segregation_rule(
				rule_command(actor_fields=["display_name"]),
				idempotency_key="COD-027-invalid-field",
			)
		self.assertEqual(invalid_field.exception.code, "IONE-CORE-0009")
		upsert_segregation_rule(
			rule_command(organization_unit=None),
			idempotency_key="COD-027-permission-rule",
		)
		frappe.db.set_value("User", TEST_USER, "enabled", 0, update_modified=False)
		with (
			patch(
				"medops.hrp_workflow_authorization.services.segregation.frappe.has_permission",
				return_value=True,
			),
			self.assertRaises(IoneApplicationError) as inactive,
		):
			validate_segregation(evaluation_command(TEST_USER))
		self.assertEqual(inactive.exception.code, "IONE-CORE-0006")
		frappe.db.set_value("User", TEST_USER, "enabled", 1, update_modified=False)
		with (
			patch(
				"medops.hrp_workflow_authorization.services.segregation.frappe.has_permission",
				return_value=True,
			),
			self.assertRaises(IoneApplicationError) as denied,
		):
			validate_segregation(evaluation_command(TEST_USER))
		self.assertEqual(denied.exception.code, "IONE-CORE-0002")

		runtime_role = "COD027 Runtime Drift Role"
		frappe.db.delete(
			"Has Role",
			{"parent": TEST_USER, "parenttype": "User", "role": runtime_role},
		)
		frappe.db.delete("Role", {"name": runtime_role})
		frappe.clear_cache(user=TEST_USER)
		frappe.get_doc({"doctype": "Role", "role_name": runtime_role}).insert(ignore_permissions=True)
		original_role_disabled = frappe.db.get_value("Role", runtime_role, "disabled")
		upsert_segregation_rule(
			rule_command(
				rule_name=TEST_RULE,
				expected_revision=1,
				actor_fields=[],
				conflicting_roles=[runtime_role],
				organization_unit=None,
			),
			idempotency_key="COD-027-runtime-role-rule",
		)
		upsert_segregation_rule(
			rule_command(
				code="COD027-TREE-ROOT",
				organization_unit=TEST_ROOT,
				include_descendants=True,
			),
			idempotency_key="COD-027-runtime-tree-rule",
		)
		create_access_scope(user=TEST_USER)
		original_unit_enabled = frappe.db.get_value("HRP Organization Unit", TEST_UNIT, "enabled")
		original_unit_valid_to = frappe.db.get_value("HRP Organization Unit", TEST_UNIT, "valid_to")
		original_version_status = frappe.db.get_value("HRP Organization Version", TEST_VERSION, "status")
		original_root_lft, original_root_rgt = frappe.db.get_value(
			"HRP Organization Unit",
			TEST_ROOT,
			["lft", "rgt"],
		)
		original_policy_digest = frappe.db.get_value("HRP Segregation Rule", TEST_RULE, "policy_digest")
		user = frappe.get_doc("User", TEST_USER)
		try:
			with patch(
				"medops.hrp_workflow_authorization.services.segregation.frappe.has_permission",
				return_value=True,
			):
				before_role = validate_segregation(evaluation_command(TEST_USER))
				self.assertTrue(before_role["allowed"])
				user.add_roles(runtime_role)
				frappe.clear_cache(user=TEST_USER)
				after_role = validate_segregation(evaluation_command(TEST_USER))
				self.assertFalse(after_role["allowed"])
				role_conflicts = cast(list[dict[str, object]], after_role["conflicts"])
				self.assertEqual(role_conflicts[0]["reason"], "INCOMPATIBLE_ROLE")
				self.assertNotEqual(
					before_role["decision_digest"],
					after_role["decision_digest"],
				)
				user.remove_roles(runtime_role)
				frappe.clear_cache(user=TEST_USER)
				after_role_removal = validate_segregation(evaluation_command(TEST_USER))
				self.assertTrue(after_role_removal["allowed"])
				self.assertIsNone(get_segregation_rule(TEST_RULE)["organization_unit"])

				frappe.db.set_value(
					"HRP Organization Unit",
					TEST_UNIT,
					"enabled",
					0,
					update_modified=False,
				)
				frappe.clear_document_cache("HRP Organization Unit", TEST_UNIT)
				with self.assertRaises(IoneApplicationError) as invalid_unit:
					validate_segregation(evaluation_command(TEST_USER))
				self.assertEqual(invalid_unit.exception.code, "IONE-CORE-0009")
				frappe.db.set_value(
					"HRP Organization Unit",
					TEST_UNIT,
					"enabled",
					original_unit_enabled,
					update_modified=False,
				)
				frappe.clear_document_cache("HRP Organization Unit", TEST_UNIT)

				frappe.db.set_value(
					"HRP Organization Unit",
					TEST_UNIT,
					"valid_to",
					"2025-12-31",
					update_modified=False,
				)
				frappe.clear_document_cache("HRP Organization Unit", TEST_UNIT)
				with self.assertRaises(IoneApplicationError) as expired_unit:
					validate_segregation(evaluation_command(TEST_USER))
				self.assertEqual(expired_unit.exception.code, "IONE-CORE-0009")
				frappe.db.set_value(
					"HRP Organization Unit",
					TEST_UNIT,
					"valid_to",
					original_unit_valid_to,
					update_modified=False,
				)
				frappe.clear_document_cache("HRP Organization Unit", TEST_UNIT)

				frappe.db.set_value(
					"HRP Organization Version",
					TEST_VERSION,
					"status",
					"Draft",
					update_modified=False,
				)
				frappe.clear_document_cache("HRP Organization Version", TEST_VERSION)
				with self.assertRaises(IoneApplicationError) as unpublished_version:
					validate_segregation(evaluation_command(TEST_USER))
				self.assertEqual(unpublished_version.exception.code, "IONE-CORE-0009")
				frappe.db.set_value(
					"HRP Organization Version",
					TEST_VERSION,
					"status",
					original_version_status,
					update_modified=False,
				)
				frappe.clear_document_cache("HRP Organization Version", TEST_VERSION)

				frappe.db.set_value(
					"HRP Organization Unit",
					TEST_ROOT,
					"rgt",
					original_root_lft,
					update_modified=False,
				)
				frappe.clear_document_cache("HRP Organization Unit", TEST_ROOT)
				with self.assertRaises(IoneApplicationError) as invalid_tree_root:
					validate_segregation(evaluation_command(TEST_USER))
				self.assertEqual(invalid_tree_root.exception.code, "IONE-CORE-0009")
				frappe.db.set_value(
					"HRP Organization Unit",
					TEST_ROOT,
					"rgt",
					original_root_rgt,
					update_modified=False,
				)
				frappe.clear_document_cache("HRP Organization Unit", TEST_ROOT)

				real_get_doc = frappe.get_doc

				def get_doc_with_invalid_effective_on(*args, **kwargs):
					doc = real_get_doc(*args, **kwargs)
					if len(args) >= 2 and args[0] == "HRP Organization Unit" and args[1] == TEST_UNIT:
						doc.valid_from = "2026-02-30"
					return doc

				with (
					patch(
						"medops.hrp_workflow_authorization.services.segregation.frappe.get_doc",
						side_effect=get_doc_with_invalid_effective_on,
					),
					self.assertRaises(IoneApplicationError) as invalid_effective_on,
				):
					validate_segregation(evaluation_command(TEST_USER))
				self.assertEqual(invalid_effective_on.exception.code, "IONE-CORE-0009")
				frappe.clear_document_cache("HRP Organization Unit", TEST_UNIT)

				frappe.db.set_value(
					"HRP Segregation Rule",
					TEST_RULE,
					"policy_digest",
					"0" * 64,
					update_modified=False,
				)
				frappe.clear_document_cache("HRP Segregation Rule", TEST_RULE)
				with self.assertRaises(IoneApplicationError) as invalid_digest:
					validate_segregation(evaluation_command(TEST_USER))
				self.assertEqual(invalid_digest.exception.code, "IONE-CORE-0009")
				with self.assertRaises(IoneApplicationError) as invalid_digest_read:
					get_segregation_rule(TEST_RULE)
				self.assertEqual(invalid_digest_read.exception.code, "IONE-CORE-0009")
				frappe.db.set_value(
					"HRP Segregation Rule",
					TEST_RULE,
					"policy_digest",
					original_policy_digest,
					update_modified=False,
				)
				frappe.clear_document_cache("HRP Segregation Rule", TEST_RULE)

				frappe.db.set_value(
					"Role",
					runtime_role,
					"disabled",
					1,
					update_modified=False,
				)
				frappe.clear_document_cache("Role", runtime_role)
				with self.assertRaises(IoneApplicationError) as disabled_role:
					validate_segregation(evaluation_command(TEST_USER))
				self.assertEqual(disabled_role.exception.code, "IONE-CORE-0009")
				with self.assertRaises(IoneApplicationError) as disabled_role_read:
					get_segregation_rule(TEST_RULE)
				self.assertEqual(disabled_role_read.exception.code, "IONE-CORE-0009")
				frappe.db.set_value(
					"Role",
					runtime_role,
					"disabled",
					original_role_disabled,
					update_modified=False,
				)
				frappe.clear_document_cache("Role", runtime_role)

				frappe.db.delete("Role", {"name": runtime_role})
				with self.assertRaises(IoneApplicationError) as invalid_role:
					validate_segregation(evaluation_command(TEST_USER))
				self.assertEqual(invalid_role.exception.code, "IONE-CORE-0009")
				with self.assertRaises(IoneApplicationError) as invalid_role_read:
					get_segregation_rule(TEST_RULE)
				self.assertEqual(invalid_role_read.exception.code, "IONE-CORE-0009")
		finally:
			frappe.db.set_value(
				"HRP Organization Unit",
				TEST_UNIT,
				"enabled",
				original_unit_enabled,
				update_modified=False,
			)
			frappe.clear_document_cache("HRP Organization Unit", TEST_UNIT)
			frappe.db.set_value(
				"HRP Organization Unit",
				TEST_UNIT,
				"valid_to",
				original_unit_valid_to,
				update_modified=False,
			)
			frappe.clear_document_cache("HRP Organization Unit", TEST_UNIT)
			frappe.db.set_value(
				"HRP Organization Version",
				TEST_VERSION,
				"status",
				original_version_status,
				update_modified=False,
			)
			frappe.clear_document_cache("HRP Organization Version", TEST_VERSION)
			frappe.db.set_value(
				"HRP Organization Unit",
				TEST_ROOT,
				{"lft": original_root_lft, "rgt": original_root_rgt},
				update_modified=False,
			)
			frappe.clear_document_cache("HRP Organization Unit", TEST_ROOT)
			frappe.db.set_value(
				"HRP Segregation Rule",
				TEST_RULE,
				"policy_digest",
				original_policy_digest,
				update_modified=False,
			)
			frappe.clear_document_cache("HRP Segregation Rule", TEST_RULE)
			if frappe.db.exists("Role", runtime_role):
				frappe.db.set_value(
					"Role",
					runtime_role,
					"disabled",
					original_role_disabled,
					update_modified=False,
				)
				frappe.clear_document_cache("Role", runtime_role)
			frappe.db.delete(
				"Has Role",
				{"parent": TEST_USER, "parenttype": "User", "role": runtime_role},
			)
			frappe.db.delete("Role", {"name": runtime_role})
			frappe.clear_cache(user=TEST_USER)


class TestSegregationAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_approval_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_segregation_state()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	@staticmethod
	def _rule_payload() -> dict[str, object]:
		return {
			"code": TEST_RULE,
			"display_name": "组织单元制单审批分离",
			"target_doctype": "HRP Organization Unit",
			"target_action": "approve",
			"company": TEST_COMPANY,
			"hospital": TEST_HOSPITAL,
			"organization_unit": TEST_UNIT,
			"actor_fields": ["owner"],
			"valid_from": "2026-01-01",
		}

	def test_http_rule_upsert_requires_idempotency_while_validation_is_live(self) -> None:
		missing_rule_key = self.post(self.method(UPSERT_METHOD), self._rule_payload())
		self.assertEqual(missing_rule_key.status_code, 400, missing_rule_key.get_data(as_text=True))

		missing_code_payload = self._rule_payload()
		missing_code_payload.pop("code")
		missing_code = self.post(
			self.method(UPSERT_METHOD),
			missing_code_payload,
			headers={"Idempotency-Key": "COD-027-http-missing-code"},
		)
		missing_user = self.post(
			self.method(VALIDATE_METHOD),
			{
				"action": "approve",
				"doctype": "HRP Organization Unit",
				"docname": TEST_UNIT,
			},
		)
		unexpected_policy = self.post(
			self.method(VALIDATE_METHOD),
			{
				"user": "Administrator",
				"action": "approve",
				"doctype": "HRP Organization Unit",
				"docname": TEST_UNIT,
				"unexpected_policy": "x",
			},
		)
		missing_rule_name = self.get(self.method(GET_METHOD), {})
		for response in (missing_code, missing_user, unexpected_policy, missing_rule_name):
			self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
			self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0003")
			self.assertEqual(response.get_json()["ione_error"]["code"], "IONE-CORE-0003")

		created = self.post(
			self.method(UPSERT_METHOD),
			self._rule_payload(),
			headers={"Idempotency-Key": "COD-027-http-rule"},
		)
		self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
		validated_without_key = self.post(
			self.method(VALIDATE_METHOD),
			{
				"user": "Administrator",
				"action": "approve",
				"doctype": "HRP Organization Unit",
				"docname": TEST_UNIT,
			},
		)
		self.assertEqual(
			validated_without_key.status_code,
			200,
			validated_without_key.get_data(as_text=True),
		)
		without_key_message = validated_without_key.get_json()["message"]
		self.assertFalse(without_key_message["allowed"])
		self.assertFalse(without_key_message["idempotency_replayed"])
		validated_with_ignored_key = self.post(
			self.method(VALIDATE_METHOD),
			{
				"user": "Administrator",
				"action": "approve",
				"doctype": "HRP Organization Unit",
				"docname": TEST_UNIT,
			},
			headers={"Idempotency-Key": "COD-027-http-validation"},
		)
		self.assertEqual(
			validated_with_ignored_key.status_code,
			200,
			validated_with_ignored_key.get_data(as_text=True),
		)
		with_key_message = validated_with_ignored_key.get_json()["message"]
		self.assertFalse(with_key_message["allowed"])
		self.assertFalse(with_key_message["idempotency_replayed"])
		self.assertEqual(
			without_key_message["decision_digest"],
			with_key_message["decision_digest"],
		)
		queried = self.get(self.method(GET_METHOD), {"rule_name": TEST_RULE})
		self.assertEqual(queried.status_code, 200, queried.get_data(as_text=True))

	def test_http_guest_is_rejected_before_contract_parsing(self) -> None:
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		with patch("medops.api.v1.core.sod.build_segregation_evaluation") as builder:
			response = self.post(
				self.method(VALIDATE_METHOD),
				{"user": "", "action": "execute", "doctype": "", "docname": ""},
			)
		self.assertEqual(response.status_code, 401, response.get_data(as_text=True))
		builder.assert_not_called()


__all__ = ["TestSegregation", "TestSegregationAPI"]
