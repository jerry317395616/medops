from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.boot import build_default_workspace_map, get_sidebar_items
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.approval_matrix import (
	build_approval_evaluation,
	build_approval_matrix_steps,
	build_approval_matrix_upsert,
)
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_workflow_authorization.permissions import (
	approval_matrix_query,
	can_read_approval_matrix,
)
from medops.hrp_workflow_authorization.services.approval_matrix import (
	UpsertApprovalMatrixService,
	evaluate_approval_matrix,
	get_approval_matrix,
	upsert_approval_matrix,
)
from medops.setup.approval_matrix import ensure_approval_matrix_governance
from medops.setup.workspaces import sync_owned_workspaces

UPSERT_METHOD = "medops.api.v1.core.approval.upsert"
EVALUATE_METHOD = "medops.api.v1.core.approval.evaluate"
GET_METHOD = "medops.api.v1.core.approval.get"
TEST_COMPANY = "COD-025测试医疗法人"
TEST_COMPANY_ABBR = "C025"
TEST_HOSPITAL = "COD025-HOSPITAL"
TEST_VERSION = "COD025-HOSPITAL-V0001"
TEST_ROOT = "COD025-HOSPITAL-V0001-COD025-HOSPITAL"
TEST_UNIT = "COD025-HOSPITAL-V0001-COD025-FIN"
TEST_MATRIX = "COD025-ORG-UNIT"
TEST_USER = "cod025-user@example.com"
TEST_PLAIN_USER = "cod025-plain@example.com"


def _ensure_user(email: str, role: str | None) -> None:
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "COD-025",
				"enabled": 1,
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
	if role and role not in frappe.get_roles(email):
		user.add_roles(role)


def ensure_approval_fixtures() -> None:
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
				"display_name": "COD-025测试医院",
				"company": TEST_COMPANY,
				"enabled": 1,
				"valid_from": "2026-01-01",
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
				"version_label": "COD-025已发布组织",
				"effective_from": "2026-01-01",
				"status": "Draft",
				"revision": 1,
				"hierarchy_digest": "0" * 64,
				"docstatus": 0,
			}
		)
		version.flags.organization_service_write = True
		version.insert(ignore_permissions=True)
	for name, code, label, parent, is_group, left, right in (
		(TEST_ROOT, TEST_HOSPITAL, "COD-025测试医院", None, 1, 1, 4),
		(TEST_UNIT, "COD025-FIN", "财务部", TEST_ROOT, 0, 2, 3),
	):
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
	_ensure_user(TEST_USER, "HRP User")
	_ensure_user(TEST_PLAIN_USER, None)
	frappe.local.db.commit()


def reset_approval_state() -> None:
	frappe.set_user("Administrator")
	frappe.db.delete("HRP Approval Matrix Row", {"parent": ("like", "COD025-%")})
	frappe.db.delete("HRP Approval Matrix", {"code": ("like", "COD025-%")})
	frappe.db.delete("HRP Access Scope Member", {"parent": ("like", "COD025-%")})
	frappe.db.delete("HRP Access Scope", {"code": ("like", "COD025-%")})
	frappe.db.delete(
		"HRP Service Idempotency",
		{"service_name": UpsertApprovalMatrixService.definition.name},
	)


def create_access_scope(*, user: str = TEST_USER) -> None:
	frappe.get_doc(
		{
			"doctype": "HRP Access Scope",
			"code": "COD025-SCOPE",
			"display_name": "COD-025审批范围",
			"company": TEST_COMPANY,
			"hospital": TEST_HOSPITAL,
			"organization_unit": TEST_UNIT,
			"enabled": 1,
			"members": [
				{
					"user": user,
					"target_doctype": "HRP Organization Unit",
					"allow_read": 1,
					"allow_write": 1,
					"enabled": 1,
				}
			],
		}
	).insert(ignore_permissions=True)


def matrix_command(
	*,
	code: str = TEST_MATRIX,
	target_doctype: str = "HRP Organization Unit",
	priority: int = 10,
	expected_revision: int = 0,
	matrix_name: str | None = None,
	display_name: str = "组织单元审批矩阵",
	valid_to: str | None = None,
):
	return build_approval_matrix_upsert(
		matrix_name=matrix_name,
		expected_revision=expected_revision,
		code=code,
		display_name=display_name,
		target_doctype=target_doctype,
		company=TEST_COMPANY,
		hospital=TEST_HOSPITAL,
		organization_unit=TEST_UNIT,
		priority=priority,
		valid_from="2026-01-01",
		valid_to=valid_to,
		steps=build_approval_matrix_steps(
			[
				{
					"sequence_no": 1,
					"step_name": "管理员审批",
					"threshold_amount": 0,
					"approver_type": "User",
					"approver_user": "Administrator",
					"approval_mode": "All",
				}
			]
		),
	)


def evaluation_command(*, amount: int = 0):
	return build_approval_evaluation(
		doctype="HRP Organization Unit",
		docname=TEST_UNIT,
		amount=amount,
		dimensions={
			"company": TEST_COMPANY,
			"hospital": TEST_HOSPITAL,
			"organization_unit": TEST_UNIT,
			"effective_on": "2026-01-01",
		},
	)


class TestApprovalMatrix(IntegrationTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_approval_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_approval_state()

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	def test_metadata_indexes_permissions_and_workspace_are_idempotent(self) -> None:
		meta = frappe.get_meta("HRP Approval Matrix")
		self.assertEqual(meta.get_field("steps").options, "HRP Approval Matrix Row")
		self.assertEqual(meta.get_field("target_doctype").label, "适用单据类型")
		self.assertSetEqual(
			{permission.role for permission in meta.permissions},
			{"System Manager", "HRP System Manager", "HRP Auditor"},
		)
		self.assertEqual(ensure_approval_matrix_governance()["schema_version"], 1)
		self.assertEqual(ensure_approval_matrix_governance()["schema_version"], 1)
		indexes = {
			str(row.Key_name)
			for row in frappe.db.sql("SHOW INDEX FROM `tabHRP Approval Matrix`", as_dict=True)
		}
		self.assertTrue(
			{"idx_hrp_approval_matrix_resolution", "idx_hrp_approval_matrix_organization"}.issubset(indexes)
		)
		self.assertEqual(sync_owned_workspaces()["schema_version"], 1)
		workspace = frappe.get_doc("Workspace", "HRP Authorization")
		self.assertIn("HRP Approval Matrix", {shortcut.link_to for shortcut in workspace.shortcuts})
		workspace_map = build_default_workspace_map(get_sidebar_items())
		self.assertEqual(workspace_map["HRP Approval Matrix"], "HRP Authorization")

	def test_create_update_replay_and_stale_revision(self) -> None:
		created = upsert_approval_matrix(matrix_command(), idempotency_key="COD-025-create-matrix")
		replayed = upsert_approval_matrix(matrix_command(), idempotency_key="COD-025-create-matrix")
		self.assertEqual(created["name"], TEST_MATRIX)
		self.assertTrue(replayed["idempotency_replayed"])
		updated = upsert_approval_matrix(
			matrix_command(
				expected_revision=1,
				matrix_name=TEST_MATRIX,
				display_name="组织单元审批矩阵修订",
			),
			idempotency_key="COD-025-update-matrix",
		)
		self.assertEqual(updated["revision"], 2)
		self.assertIn("display_name", updated["changed_fields"])
		with self.assertRaises(IoneApplicationError) as raised:
			upsert_approval_matrix(
				matrix_command(expected_revision=1, matrix_name=TEST_MATRIX),
				idempotency_key="COD-025-stale-matrix",
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0005")

	def test_evaluate_resolves_real_document_and_is_deterministic(self) -> None:
		upsert_approval_matrix(matrix_command(), idempotency_key="COD-025-evaluate-matrix")
		first = evaluate_approval_matrix(evaluation_command())
		second = evaluate_approval_matrix(evaluation_command())
		self.assertEqual(first["matrix"], TEST_MATRIX)
		self.assertEqual(first["approvers"], ["Administrator"])
		self.assertEqual(first["decision_digest"], second["decision_digest"])
		self.assertFalse(first["idempotency_replayed"])

	def test_evaluate_rejects_tampered_context_and_ambiguous_policy(self) -> None:
		upsert_approval_matrix(matrix_command(), idempotency_key="COD-025-primary-matrix")
		upsert_approval_matrix(
			matrix_command(code="COD025-AMBIGUOUS"),
			idempotency_key="COD-025-ambiguous-matrix",
		)
		with self.assertRaises(IoneApplicationError) as ambiguous:
			evaluate_approval_matrix(evaluation_command())
		self.assertEqual(ambiguous.exception.code, "IONE-CORE-0009")
		with self.assertRaises(IoneApplicationError) as tampered:
			evaluate_approval_matrix(evaluation_command(amount=1))
		self.assertEqual(tampered.exception.code, "IONE-CORE-0005")

	def test_user_requires_role_scope_and_document_permission(self) -> None:
		upsert_approval_matrix(matrix_command(), idempotency_key="COD-025-scope-matrix")
		create_access_scope()
		frappe.set_user(TEST_USER)
		with patch(
			"medops.hrp_workflow_authorization.services.approval_matrix.frappe.has_permission",
			return_value=True,
		):
			decision = evaluate_approval_matrix(evaluation_command())
		self.assertEqual(decision["matrix"], TEST_MATRIX)
		frappe.set_user(TEST_PLAIN_USER)
		with self.assertRaises(IoneApplicationError) as denied:
			evaluate_approval_matrix(evaluation_command())
		self.assertEqual(denied.exception.code, "IONE-CORE-0002")

	def test_matrix_permission_helpers_are_fail_closed(self) -> None:
		upsert_approval_matrix(matrix_command(), idempotency_key="COD-025-permission-matrix")
		doc = frappe.get_doc("HRP Approval Matrix", TEST_MATRIX)
		self.assertEqual(approval_matrix_query(TEST_PLAIN_USER), "1=0")
		self.assertTrue(can_read_approval_matrix(doc, "Administrator", "write"))
		self.assertFalse(can_read_approval_matrix(doc, TEST_PLAIN_USER, "read"))
		payload = get_approval_matrix(TEST_MATRIX)
		self.assertEqual(payload["policy_digest"], doc.policy_digest)

	def test_configuration_rejects_missing_dimensions_and_invalid_validity(self) -> None:
		with self.assertRaises(IoneApplicationError) as missing_dimensions:
			upsert_approval_matrix(
				matrix_command(target_doctype="HRP Feature Flag"),
				idempotency_key="COD-025-invalid-target",
			)
		self.assertEqual(missing_dimensions.exception.code, "IONE-CORE-0009")

		frappe.db.set_value("HRP Organization Unit", TEST_UNIT, "valid_to", "2026-12-31")
		try:
			with self.assertRaises(IoneApplicationError) as invalid_validity:
				upsert_approval_matrix(
					matrix_command(code="COD025-UNBOUNDED"),
					idempotency_key=".".join(("test", "cod025", "validity")),
				)
			self.assertEqual(invalid_validity.exception.code, "IONE-CORE-0005")
		finally:
			frappe.db.set_value("HRP Organization Unit", TEST_UNIT, "valid_to", None)


class TestApprovalMatrixAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_approval_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_approval_state()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	@staticmethod
	def _payload() -> dict[str, object]:
		return {
			"code": TEST_MATRIX,
			"display_name": "组织单元审批矩阵",
			"target_doctype": "HRP Organization Unit",
			"company": TEST_COMPANY,
			"hospital": TEST_HOSPITAL,
			"organization_unit": TEST_UNIT,
			"priority": 10,
			"valid_from": "2026-01-01",
			"steps": [
				{
					"sequence_no": 1,
					"step_name": "管理员审批",
					"threshold_amount": 0,
					"approver_type": "User",
					"approver_user": "Administrator",
					"approval_mode": "All",
				}
			],
		}

	def test_http_upsert_requires_idempotency_and_evaluate_is_read_only(self) -> None:
		missing_key = self.post(self.method(UPSERT_METHOD), self._payload())
		self.assertEqual(missing_key.status_code, 400, missing_key.get_data(as_text=True))
		created = self.post(
			self.method(UPSERT_METHOD),
			self._payload(),
			headers={"Idempotency-Key": "COD-025-http-create"},
		)
		self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
		evaluated = self.post(
			self.method(EVALUATE_METHOD),
			{
				"doctype": "HRP Organization Unit",
				"docname": TEST_UNIT,
				"amount": 0,
				"dimensions": {
					"company": TEST_COMPANY,
					"hospital": TEST_HOSPITAL,
					"organization_unit": TEST_UNIT,
					"effective_on": "2026-01-01",
				},
			},
		)
		self.assertEqual(evaluated.status_code, 200, evaluated.get_data(as_text=True))
		self.assertEqual(evaluated.get_json()["message"]["matrix"], TEST_MATRIX)
		queried = self.get(self.method(GET_METHOD), {"matrix_name": TEST_MATRIX})
		self.assertEqual(queried.status_code, 200, queried.get_data(as_text=True))

	def test_http_guest_is_rejected_before_contract_parsing(self) -> None:
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		with patch("medops.api.v1.core.approval.build_approval_evaluation") as builder:
			response = self.post(
				self.method(EVALUATE_METHOD),
				{"doctype": "Missing", "docname": "Missing", "amount": 0, "dimensions": {}},
			)
		self.assertEqual(response.status_code, 401, response.get_data(as_text=True))
		builder.assert_not_called()


__all__ = ["TestApprovalMatrix", "TestApprovalMatrixAPI"]
