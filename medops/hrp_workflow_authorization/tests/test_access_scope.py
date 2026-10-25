from __future__ import annotations

from typing import cast
from unittest.mock import patch

import frappe
from frappe.boot import build_default_workspace_map, get_sidebar_items
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.error_catalog import IoneApplicationError
from medops.hrp_workflow_authorization.permissions import (
	has_scoped_permission,
	organization_unit_query,
)
from medops.hrp_workflow_authorization.services.access_scope import resolve_access_scope
from medops.setup.access_scope import ensure_access_scope_governance
from medops.setup.workspaces import sync_owned_workspaces

RESOLVE_METHOD = "medops.api.v1.core.scope.resolve"
TEST_COMPANY = "COD-024测试医疗法人"
TEST_COMPANY_ABBR = "C024"
TEST_HOSPITAL = "COD024-HOSPITAL"
TEST_VERSION = "COD024-HOSPITAL-V0001"
TEST_ROOT = "COD024-HOSPITAL-V0001-COD024-HOSPITAL"
TEST_DEPARTMENT = "COD024-HOSPITAL-V0001-COD024-FIN"
TEST_CHILD = "COD024-HOSPITAL-V0001-COD024-AP"
TEST_USER = "cod024-user@example.com"
TEST_OTHER_USER = "cod024-other@example.com"
TEST_SCOPE = "COD024-FIN"


def _ensure_user(email: str, role: str = "HRP Data Steward") -> None:
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "COD-024",
				"enabled": 1,
				"send_welcome_email": 0,
			}
		)
		user.insert(ignore_permissions=True)
	if role not in frappe.get_roles(email):
		user.add_roles(role)


def ensure_access_scope_fixtures() -> None:
	frappe.set_user("Administrator")
	if not frappe.db.exists("Warehouse Type", "Transit"):
		frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit", "description": "中转仓"}).insert(
			ignore_permissions=True
		)
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
	if not frappe.db.exists("HRP Hospital", TEST_HOSPITAL):
		hospital = frappe.get_doc(
			{
				"doctype": "HRP Hospital",
				"code": TEST_HOSPITAL,
				"display_name": "COD-024测试医院",
				"company": TEST_COMPANY,
				"enabled": 1,
				"revision": 1,
				"next_version_number": 2,
			}
		)
		hospital.flags.organization_migration = True
		hospital.insert(ignore_permissions=True)
	if not frappe.db.exists("HRP Organization Version", TEST_VERSION):
		version = frappe.get_doc(
			{
				"doctype": "HRP Organization Version",
				"name": TEST_VERSION,
				"hospital": TEST_HOSPITAL,
				"company": TEST_COMPANY,
				"version_number": 1,
				"version_code": "V0001",
				"version_label": "COD-024已发布组织",
				"effective_from": "2026-01-01",
				"status": "Draft",
				"revision": 1,
				"hierarchy_digest": "0" * 64,
				"docstatus": 0,
			}
		)
		version.flags.organization_service_write = True
		version.insert(ignore_permissions=True)
	for values in (
		(TEST_ROOT, TEST_HOSPITAL, "COD-024测试医院", None, 1, 1, 6),
		(TEST_DEPARTMENT, "COD024-FIN", "财务部", TEST_ROOT, 1, 2, 5),
		(TEST_CHILD, "COD024-AP", "应付组", TEST_DEPARTMENT, 0, 3, 4),
	):
		name, code, label, parent, is_group, left, right = values
		if frappe.db.exists("HRP Organization Unit", name):
			continue
		unit = frappe.get_doc(
			{
				"doctype": "HRP Organization Unit",
				"name": name,
				"organization_version": TEST_VERSION,
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"code": code,
				"display_name": label,
				"unit_type": "HOSPITAL" if parent is None else "ADMINISTRATIVE_DEPARTMENT",
				"parent_organization_unit": parent,
				"is_group": is_group,
				"enabled": 1,
				"sequence": 1,
				"valid_from": "2026-01-01",
				"lft": left,
				"rgt": right,
			}
		)
		unit.flags.organization_service_write = True
		unit.insert(ignore_permissions=True)
	frappe.db.set_value("HRP Organization Version", TEST_VERSION, "status", "Published")
	frappe.db.set_value("HRP Organization Version", TEST_VERSION, "docstatus", 1)
	_ensure_user(TEST_USER)
	_ensure_user(TEST_OTHER_USER)
	frappe.local.db.commit()


def reset_scope() -> None:
	frappe.set_user("Administrator")
	frappe.db.delete("HRP Access Scope Member", {"parent": TEST_SCOPE})
	frappe.db.delete("HRP Access Scope", {"name": TEST_SCOPE})


def create_scope(*, include_descendants: bool = True, target_doctype: str | None = None):
	return frappe.get_doc(
		{
			"doctype": "HRP Access Scope",
			"code": TEST_SCOPE,
			"display_name": "财务部访问范围",
			"company": TEST_COMPANY,
			"hospital": TEST_HOSPITAL,
			"organization_unit": TEST_DEPARTMENT,
			"include_descendants": int(include_descendants),
			"enabled": 1,
			"members": [
				{
					"user": TEST_USER,
					"target_doctype": target_doctype,
					"allow_read": 1,
					"allow_write": 1,
					"enabled": 1,
				}
			],
		}
	).insert(ignore_permissions=True)


class TestAccessScope(IntegrationTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_access_scope_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_scope()

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	def test_metadata_indexes_and_owned_workspaces_are_idempotent(self) -> None:
		meta = frappe.get_meta("HRP Access Scope")
		member_meta = frappe.get_meta("HRP Access Scope Member")
		self.assertEqual(meta.get_field("members").options, "HRP Access Scope Member")
		self.assertEqual(member_meta.get_field("allow_read").label, "允许读取")
		self.assertSetEqual(
			{permission.role for permission in meta.permissions},
			{"System Manager", "HRP System Manager", "HRP Auditor"},
		)
		self.assertEqual(ensure_access_scope_governance()["schema_version"], 1)
		self.assertEqual(ensure_access_scope_governance()["schema_version"], 1)
		indexes = {
			str(row.Key_name) for row in frappe.db.sql("SHOW INDEX FROM `tabHRP Access Scope`", as_dict=True)
		}
		self.assertTrue(
			{"idx_hrp_access_scope_effectivity", "idx_hrp_access_scope_organization"}.issubset(indexes)
		)

		frappe.db.set_value("Workspace", "HRP", "modified", "2099-01-01 00:00:00")
		self.assertEqual(sync_owned_workspaces()["schema_version"], 1)
		self.assertEqual(sync_owned_workspaces()["schema_version"], 1)
		workspace = frappe.get_doc("Workspace", "HRP Authorization")
		self.assertIn("HRP Access Scope", {shortcut.link_to for shortcut in workspace.shortcuts})
		workspace_map = build_default_workspace_map(get_sidebar_items())
		self.assertEqual(workspace_map["HRP Access Scope"], "HRP Authorization")

	def test_scope_validates_published_tree_and_increments_revision(self) -> None:
		doc = create_scope()
		self.assertEqual(doc.scope_level, "Organization Unit")
		self.assertEqual(doc.revision, 1)
		first_digest = doc.policy_digest
		doc.display_name = "财务与应付访问范围"
		doc.save(ignore_permissions=True)
		self.assertEqual(doc.revision, 2)
		self.assertNotEqual(doc.policy_digest, first_digest)

	def test_resolver_expands_descendants_and_respects_target_doctype(self) -> None:
		create_scope(target_doctype="HRP Organization Unit")
		frappe.set_user(TEST_USER)
		decision = resolve_access_scope(
			user=None,
			doctype="HRP Organization Unit",
			action="read",
		)
		self.assertTrue(decision["allowed"])
		filters = cast(dict[str, object], decision["filters"])
		groups = cast(list[dict[str, object]], filters["groups"])
		conditions = cast(list[dict[str, object]], groups[0]["conditions"])
		unit_condition = next(row for row in conditions if row["field"] == "name")
		self.assertEqual(unit_condition["operator"], "in")
		self.assertEqual(unit_condition["value"], [TEST_DEPARTMENT, TEST_CHILD])
		other = resolve_access_scope(user=None, doctype="HRP Hospital", action="read")
		self.assertFalse(other["allowed"])

	def test_resolver_never_grants_missing_base_permission(self) -> None:
		create_scope()
		frappe.set_user(TEST_USER)
		with patch(
			"medops.hrp_workflow_authorization.services.access_scope.frappe.has_permission",
			return_value=False,
		):
			decision = resolve_access_scope(user=None, doctype="HRP Organization Unit", action="read")
		self.assertFalse(decision["allowed"])
		self.assertFalse(decision["base_permission"])

	def test_non_admin_cannot_resolve_another_user(self) -> None:
		frappe.set_user(TEST_USER)
		with self.assertRaises(IoneApplicationError) as raised:
			resolve_access_scope(user=TEST_OTHER_USER, doctype="HRP Hospital", action="read")
		self.assertEqual(raised.exception.code, "IONE-CORE-0002")

	def test_permission_helpers_filter_lists_and_documents(self) -> None:
		create_scope()
		query = organization_unit_query(TEST_USER)
		self.assertIn("`tabHRP Organization Unit`.`name` IN", query)
		self.assertIn(frappe.db.escape(TEST_CHILD), query)
		child = frappe.get_doc("HRP Organization Unit", TEST_CHILD)
		root = frappe.get_doc("HRP Organization Unit", TEST_ROOT)
		self.assertTrue(has_scoped_permission(child, TEST_USER, "read"))
		self.assertFalse(has_scoped_permission(root, TEST_USER, "read"))
		self.assertEqual(organization_unit_query("Guest"), "1=0")

	def test_admin_is_unrestricted_and_audit_uses_digest(self) -> None:
		frappe.set_user("Administrator")
		with patch("medops.services.audit_context.frappe.logger") as logger:
			decision = resolve_access_scope(
				user=TEST_USER,
				doctype="HRP Organization Unit",
				action="read",
			)
		self.assertFalse(decision["allowed"])
		payload = " ".join(
			str(call.args[0])
			for level in (logger.return_value.info, logger.return_value.warning)
			for call in level.call_args_list
		)
		self.assertNotIn(TEST_USER, payload)


class TestAccessScopeAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_access_scope_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_scope()
		create_scope(target_doctype="HRP Organization Unit")
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def test_http_resolve_is_read_only_and_needs_no_idempotency_key(self) -> None:
		response = self.post(
			self.method(RESOLVE_METHOD),
			{"user": TEST_USER, "doctype": "HRP Organization Unit", "action": "read"},
			headers={"X-Correlation-ID": "COD-024-http-resolve"},
		)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		payload = response.get_json()["message"]
		self.assertTrue(payload["allowed"])
		self.assertEqual(payload["user"], TEST_USER)
		self.assertTrue(response.headers["X-Correlation-ID"])

	def test_http_invalid_action_is_controlled(self) -> None:
		response = self.post(
			self.method(RESOLVE_METHOD),
			{"doctype": "HRP Organization Unit", "action": "delete"},
		)
		self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
		self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0003")


__all__ = ["TestAccessScope", "TestAccessScopeAPI"]
