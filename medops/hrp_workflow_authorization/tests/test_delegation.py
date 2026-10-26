from __future__ import annotations

from datetime import date, timedelta
from typing import Any, cast
from unittest.mock import patch

import frappe
from frappe.boot import build_default_workspace_map, get_sidebar_items
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase
from frappe.utils import getdate, nowdate

from medops.common.delegation import build_delegation_create, build_delegation_revoke
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_workflow_authorization.permissions import can_read_delegation, delegation_query
from medops.hrp_workflow_authorization.services.approval_matrix import (
	UpsertApprovalMatrixService,
	evaluate_approval_matrix,
	upsert_approval_matrix,
)
from medops.hrp_workflow_authorization.services.delegation import (
	CreateDelegationService,
	RevokeDelegationService,
	create_delegation,
	get_delegation,
	revoke_delegation,
	sync_delegation_statuses,
)
from medops.hrp_workflow_authorization.tests.test_approval_matrix import (
	TEST_MATRIX,
	TEST_PLAIN_USER,
	TEST_USER,
	create_access_scope,
	ensure_approval_fixtures,
	evaluation_command,
	matrix_command,
)
from medops.setup.delegation import ensure_delegation_governance
from medops.setup.workspaces import sync_owned_workspaces

CREATE_METHOD = "medops.api.v1.core.delegation.create"
REVOKE_METHOD = "medops.api.v1.core.delegation.revoke"
GET_METHOD = "medops.api.v1.core.delegation.get"


def test_request_id(purpose: str) -> str:
	return "-".join(("test", "cod026", purpose))


class _FutureDate(date):
	@classmethod
	def today(cls) -> _FutureDate:
		future = getdate(nowdate()) + timedelta(days=31)
		return cls(future.year, future.month, future.day)


def reset_delegation_state() -> None:
	frappe.set_user("Administrator")
	frappe.db.delete("HRP Delegation", {"matrix": ("like", "COD025-%")})
	frappe.db.delete("HRP Approval Matrix Row", {"parent": ("like", "COD025-%")})
	frappe.db.delete("HRP Approval Matrix", {"code": ("like", "COD025-%")})
	frappe.db.delete("HRP Access Scope Member", {"parent": ("like", "COD025-%")})
	frappe.db.delete("HRP Access Scope", {"code": ("like", "COD025-%")})
	frappe.db.delete(
		"HRP Service Idempotency",
		{
			"service_name": (
				"in",
				[
					UpsertApprovalMatrixService.definition.name,
					CreateDelegationService.definition.name,
					RevokeDelegationService.definition.name,
				],
			)
		},
	)


def delegation_command(*, to_user: str = TEST_USER, step_sequence: int | None = None):
	today = getdate(nowdate())
	return build_delegation_create(
		matrix=TEST_MATRIX,
		from_user="Administrator",
		to_user=to_user,
		valid_from=today.isoformat(),
		valid_to=(today + timedelta(days=30)).isoformat(),
		step_sequence=step_sequence,
		reason="COD-026 自动化验证",
	)


def prepare_delegation_policy() -> None:
	frappe.db.set_value("User", TEST_PLAIN_USER, "user_type", "System User")
	upsert_approval_matrix(matrix_command(), idempotency_key=test_request_id("matrix"))
	create_access_scope()


class TestDelegation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_approval_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_delegation_state()
		prepare_delegation_policy()

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	def _create(self, *, key: str | None = None) -> dict[str, object]:
		with patch(
			"medops.hrp_workflow_authorization.services.delegation.frappe.has_permission",
			return_value=True,
		):
			return create_delegation(
				delegation_command(),
				idempotency_key=key or test_request_id("create"),
			)

	def test_metadata_indexes_permissions_workspace_and_direct_write_guard(self) -> None:
		meta = frappe.get_meta("HRP Delegation")
		self.assertEqual(meta.get_field("matrix").label, "审批矩阵")
		self.assertEqual(meta.get_field("from_user").label, "委托人")
		self.assertSetEqual(
			{permission.role for permission in meta.permissions},
			{
				"System Manager",
				"HRP System Manager",
				"HRP Department Manager",
				"HRP User",
				"HRP Auditor",
			},
		)
		self.assertEqual(ensure_delegation_governance()["schema_version"], 1)
		indexes = {
			str(row.Key_name) for row in frappe.db.sql("SHOW INDEX FROM `tabHRP Delegation`", as_dict=True)
		}
		self.assertTrue({"idx_hrp_delegation_resolution", "idx_hrp_delegation_principals"}.issubset(indexes))
		self.assertEqual(sync_owned_workspaces()["schema_version"], 1)
		workspace = frappe.get_doc("Workspace", "HRP Authorization")
		self.assertIn("HRP Delegation", {shortcut.link_to for shortcut in workspace.shortcuts})
		self.assertEqual(
			build_default_workspace_map(get_sidebar_items())["HRP Delegation"],
			"HRP Authorization",
		)
		with self.assertRaises(IoneApplicationError) as direct:
			matrix = frappe.get_doc("HRP Approval Matrix", TEST_MATRIX)
			frappe.get_doc(
				{
					"doctype": "HRP Delegation",
					"matrix": TEST_MATRIX,
					"matrix_revision": matrix.revision,
					"matrix_digest": matrix.policy_digest,
					"target_doctype": matrix.target_doctype,
					"company": matrix.company,
					"hospital": matrix.hospital,
					"organization_unit": matrix.organization_unit,
					"include_descendants": matrix.include_descendants,
					"from_user": "Administrator",
					"to_user": TEST_USER,
					"valid_from": "2026-01-01",
					"valid_to": "2026-01-31",
					"status": "Active",
				}
			).insert(ignore_permissions=True)
		self.assertEqual(direct.exception.code, "IONE-CORE-0008")

	def test_create_replay_overlap_get_and_permission_filter(self) -> None:
		with (
			patch(
				"medops.hrp_workflow_authorization.services.delegation.frappe.has_permission",
				return_value=True,
			),
			self.assertRaises(IoneApplicationError) as denied,
		):
			create_delegation(
				delegation_command(to_user=TEST_PLAIN_USER),
				idempotency_key=test_request_id("missing-scope"),
			)
		self.assertEqual(denied.exception.code, "IONE-CORE-0002")
		created = self._create()
		replayed = self._create()
		self.assertTrue(str(created["name"]).startswith("DLG-2026-"))
		self.assertTrue(replayed["idempotency_replayed"])
		with (
			patch(
				"medops.hrp_workflow_authorization.services.delegation.frappe.has_permission",
				return_value=True,
			),
			self.assertRaises(IoneApplicationError) as overlap,
		):
			create_delegation(
				delegation_command(),
				idempotency_key=test_request_id("overlap"),
			)
		self.assertEqual(overlap.exception.code, "IONE-CORE-0005")
		queried = get_delegation(str(created["name"]))
		self.assertEqual(queried["matrix_revision"], 1)
		doc = frappe.get_doc("HRP Delegation", created["name"])
		self.assertIn("from_user", delegation_query(TEST_USER))
		self.assertTrue(can_read_delegation(doc, TEST_USER, "read"))
		self.assertFalse(can_read_delegation(doc, TEST_PLAIN_USER, "read"))

	def test_evaluation_applies_then_revocation_restores_original_approver(self) -> None:
		created = self._create()
		with patch(
			"medops.hrp_workflow_authorization.services.delegation.frappe.has_permission",
			return_value=True,
		):
			delegated = evaluate_approval_matrix(evaluation_command())
		self.assertEqual(delegated["approvers"], [TEST_USER])
		self.assertEqual(delegated["delegations"], [created["name"]])
		steps = cast(list[dict[str, Any]], delegated["steps"])
		self.assertEqual(steps[0]["approvers"][0].get("delegated_from"), "Administrator")
		revoked = revoke_delegation(
			build_delegation_revoke(delegation=created["name"], reason="委托提前结束"),
			idempotency_key=test_request_id("revoke"),
		)
		self.assertEqual(revoked["status"], "Revoked")
		replayed = revoke_delegation(
			build_delegation_revoke(delegation=created["name"], reason="委托提前结束"),
			idempotency_key=test_request_id("revoke"),
		)
		self.assertTrue(replayed["idempotency_replayed"])
		with patch(
			"medops.hrp_workflow_authorization.services.delegation.frappe.has_permission",
			return_value=True,
		):
			original = evaluate_approval_matrix(evaluation_command())
		self.assertEqual(original["approvers"], ["Administrator"])
		self.assertEqual(original["delegations"], [])

	def test_matrix_revision_change_invalidates_existing_delegation(self) -> None:
		created = self._create()
		upsert_approval_matrix(
			matrix_command(
				matrix_name=TEST_MATRIX,
				expected_revision=1,
				display_name="COD-026 修订矩阵",
			),
			idempotency_key=test_request_id("revise-matrix"),
		)
		with patch(
			"medops.hrp_workflow_authorization.services.delegation.frappe.has_permission",
			return_value=True,
		):
			decision = evaluate_approval_matrix(evaluation_command())
		self.assertEqual(decision["approvers"], ["Administrator"])
		self.assertNotIn(created["name"], decision["delegations"])

	def test_status_job_activates_and_expires_without_touching_revoked_records(self) -> None:
		created = self._create()
		frappe.db.set_value("HRP Delegation", created["name"], "status", "Scheduled")
		with patch(
			"medops.hrp_workflow_authorization.services.delegation.frappe.has_permission",
			return_value=True,
		):
			decision = evaluate_approval_matrix(evaluation_command())
		self.assertEqual(decision["approvers"], [TEST_USER])
		self.assertEqual(sync_delegation_statuses()["activated"], 1)
		with patch(
			"medops.hrp_workflow_authorization.services.delegation.nowdate",
			return_value=_FutureDate.today().isoformat(),
		):
			self.assertEqual(sync_delegation_statuses()["expired"], 1)
		self.assertEqual(frappe.db.get_value("HRP Delegation", created["name"], "status"), "Expired")


class TestDelegationAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_approval_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_delegation_state()
		prepare_delegation_policy()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def test_http_create_get_revoke_require_idempotency_for_writes(self) -> None:
		today = getdate(nowdate())
		payload = {
			"matrix": TEST_MATRIX,
			"from_user": "Administrator",
			"to_user": TEST_USER,
			"valid_from": today.isoformat(),
			"valid_to": (today + timedelta(days=30)).isoformat(),
			"reason": "接口自动化验证",
		}
		with patch(
			"medops.hrp_workflow_authorization.services.delegation.frappe.has_permission",
			return_value=True,
		):
			missing = self.post(self.method(CREATE_METHOD), payload)
			self.assertEqual(missing.status_code, 400, missing.get_data(as_text=True))
			created = self.post(
				self.method(CREATE_METHOD),
				payload,
				headers={"Idempotency-Key": test_request_id("http-create")},
			)
		self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
		name = created.get_json()["message"]["name"]
		queried = self.get(self.method(GET_METHOD), {"delegation": name})
		self.assertEqual(queried.status_code, 200, queried.get_data(as_text=True))
		revoked = self.post(
			self.method(REVOKE_METHOD),
			{"delegation": name, "reason": "接口撤销"},
			headers={"Idempotency-Key": test_request_id("http-revoke")},
		)
		self.assertEqual(revoked.status_code, 200, revoked.get_data(as_text=True))
		self.assertEqual(revoked.get_json()["message"]["status"], "Revoked")

	def test_http_guest_is_rejected_before_contract_parsing(self) -> None:
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		with patch("medops.api.v1.core.delegation.build_delegation_create") as builder:
			response = self.post(self.method(CREATE_METHOD), {})
		self.assertEqual(response.status_code, 401, response.get_data(as_text=True))
		builder.assert_not_called()


__all__ = ["TestDelegation", "TestDelegationAPI"]
