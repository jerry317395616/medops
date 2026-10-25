from __future__ import annotations

import json
from typing import cast
from unittest.mock import patch

import frappe
from frappe.boot import build_default_workspace_map, get_sidebar_items
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.domain_service import idempotency_record_name
from medops.common.error_catalog import IoneApplicationError
from medops.common.master_data import (
	build_master_data_domain_upsert,
	build_master_data_request_review,
	build_master_data_request_submit,
	build_master_data_request_upsert,
)
from medops.common.organization import (
	build_hierarchy_replace,
	build_hospital_upsert,
	build_organization_version_create,
	build_organization_version_publish,
)
from medops.hrp_master_data.services.master_data import (
	ReviewMasterDataRequestService,
	SaveMasterDataRequestService,
	SubmitMasterDataRequestService,
	UpsertMasterDataDomainService,
	get_master_data_request,
	review_master_data_request,
	save_master_data_request,
	submit_master_data_request,
	upsert_master_data_domain,
)
from medops.hrp_organization.services.organization import (
	CreateOrganizationVersionService,
	PublishOrganizationVersionService,
	ReplaceOrganizationHierarchyService,
	UpsertHospitalService,
	create_organization_version,
	publish_organization_version,
	replace_organization_hierarchy,
	upsert_hospital,
)
from medops.setup.master_data import ensure_master_data_governance

UPSERT_DOMAIN_METHOD = "medops.api.v1.master_data.upsert_master_data_domain"
SAVE_REQUEST_METHOD = "medops.api.v1.master_data.save_master_data_request"
SUBMIT_REQUEST_METHOD = "medops.api.v1.master_data.submit_master_data_request"
REVIEW_REQUEST_METHOD = "medops.api.v1.master_data.review_master_data_request"
GET_REQUEST_METHOD = "medops.api.v1.master_data.get_master_data_request"
UPSERT_HOSPITAL_METHOD = "medops.api.v1.organization.upsert_hospital"
CREATE_VERSION_METHOD = "medops.api.v1.organization.create_organization_version"
REPLACE_HIERARCHY_METHOD = "medops.api.v1.organization.replace_organization_hierarchy"
PUBLISH_VERSION_METHOD = "medops.api.v1.organization.publish_organization_version"

TEST_DOMAIN = "COD020-ITEM"
TEST_ITEM = "COD020-ITEM-001"
TEST_REQUESTER = "cod020-requester@example.com"
TEST_REVIEWER = "cod020-reviewer@example.com"
TEST_COMPANY = "COD-020测试医疗法人"
TEST_COMPANY_ABBR = "C020"
TEST_HOSPITAL = "COD020-HOSPITAL"
TEST_ITEM_GROUP = "COD020 Items"
TEST_ITEM_GROUP_ROOT = "COD020 All Item Groups"
TEST_STOCK_UOM = "COD020-UOM"
TEST_WAREHOUSE_TYPE = "Transit"
MASTER_DATA_SERVICE_NAMES = (
	UpsertMasterDataDomainService.definition.name,
	SaveMasterDataRequestService.definition.name,
	SubmitMasterDataRequestService.definition.name,
	ReviewMasterDataRequestService.definition.name,
)
ORGANIZATION_SERVICE_NAMES = (
	UpsertHospitalService.definition.name,
	CreateOrganizationVersionService.definition.name,
	ReplaceOrganizationHierarchyService.definition.name,
	PublishOrganizationVersionService.definition.name,
)


def ensure_company_fixture() -> None:
	if not frappe.db.exists("Warehouse Type", TEST_WAREHOUSE_TYPE):
		frappe.get_doc(
			{
				"doctype": "Warehouse Type",
				"name": TEST_WAREHOUSE_TYPE,
				"description": "COD-020测试中转仓类型",
			}
		).insert(ignore_permissions=True)
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


def hierarchy_nodes() -> list[dict[str, object]]:
	return [
		{
			"code": TEST_HOSPITAL,
			"display_name": "COD-020测试医院",
			"unit_type": "HOSPITAL",
			"parent_code": None,
			"is_group": 1,
			"enabled": 1,
			"sequence": 1,
			"valid_from": "2026-01-01",
		},
		{
			"code": "OUTPATIENT",
			"display_name": "门诊部",
			"unit_type": "CLINICAL_DEPARTMENT",
			"parent_code": TEST_HOSPITAL,
			"is_group": 0,
			"enabled": 1,
			"sequence": 1,
			"valid_from": "2026-01-01",
		},
	]


def create_version(*, suffix: str) -> dict[str, object]:
	upsert_hospital(
		build_hospital_upsert(
			code=TEST_HOSPITAL,
			company=TEST_COMPANY,
			display_name="COD-020测试医院",
			expected_revision=0,
		),
		idempotency_key=f"COD-020-hospital-{suffix}",
	)
	version = create_organization_version(
		build_organization_version_create(
			hospital=TEST_HOSPITAL,
			effective_from="2026-01-01",
			version_label="COD-020组织版本",
		),
		idempotency_key=f"COD-020-version-{suffix}",
	)
	replaced = replace_organization_hierarchy(
		build_hierarchy_replace(
			organization_version=version["name"],
			expected_revision=version["revision"],
			nodes=hierarchy_nodes(),
		),
		idempotency_key=f"COD-020-hierarchy-{suffix}",
	)
	return publish_organization_version(
		build_organization_version_publish(
			organization_version=version["name"],
			expected_revision=replaced["revision"],
		),
		idempotency_key=f"COD-020-publish-{suffix}",
	)


def unit_name(version: str, code: str) -> str:
	name = frappe.db.get_value(
		"HRP Organization Unit",
		{"organization_version": version, "code": code},
		"name",
	)
	if not name:
		raise RuntimeError(f"organization unit {code} is missing")
	return str(name)


def reset_organization_state() -> None:
	version_names = frappe.get_all(
		"HRP Organization Version",
		filters={"hospital": TEST_HOSPITAL},
		pluck="name",
	)
	if version_names:
		frappe.db.delete(
			"HRP Organization Unit",
			{"organization_version": ("in", version_names)},
		)
		frappe.db.delete(
			"HRP Organization Version",
			{"name": ("in", version_names)},
		)
	frappe.db.delete("HRP Hospital", {"name": TEST_HOSPITAL})
	frappe.db.delete(
		"HRP Service Idempotency",
		{"service_name": ("in", ORGANIZATION_SERVICE_NAMES)},
	)


def ensure_master_data_fixtures() -> None:
	ensure_company_fixture()
	for email, last_name, role in (
		(TEST_REQUESTER, "申请人", "HRP User"),
		(TEST_REVIEWER, "审核人", "HRP Data Steward"),
	):
		if frappe.db.exists("User", email):
			user = frappe.get_doc("User", email)
		else:
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "COD-020",
					"last_name": last_name,
					"enabled": 1,
					"send_welcome_email": 0,
				}
			)
			user.insert(ignore_permissions=True)
		if role not in frappe.get_roles(email):
			user.add_roles(role)
	if not frappe.db.exists("UOM", TEST_STOCK_UOM):
		frappe.get_doc(
			{
				"doctype": "UOM",
				"uom_name": TEST_STOCK_UOM,
				"must_be_whole_number": 0,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Item Group", TEST_ITEM_GROUP):
		root_item_group = frappe.db.get_value(
			"Item Group",
			{"parent_item_group": ("is", "not set")},
			"name",
		)
		if not root_item_group:
			root_item_group = (
				frappe.get_doc(
					{
						"doctype": "Item Group",
						"item_group_name": TEST_ITEM_GROUP_ROOT,
						"is_group": 1,
					}
				)
				.insert(ignore_permissions=True)
				.name
			)
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": TEST_ITEM_GROUP,
				"parent_item_group": root_item_group,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Item", TEST_ITEM):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": TEST_ITEM,
				"item_name": "COD-020原始物料",
				"item_group": TEST_ITEM_GROUP,
				"stock_uom": TEST_STOCK_UOM,
				"is_stock_item": 0,
				"disabled": 0,
			}
		).insert(ignore_permissions=True)
	frappe.local.db.commit()


def reset_master_data_state() -> None:
	frappe.set_user("Administrator")
	request_names = frappe.get_all(
		"HRP Master Data Request",
		filters={"master_data_domain": TEST_DOMAIN},
		pluck="name",
	)
	if request_names:
		frappe.db.delete(
			"HRP Master Data Change Item",
			{"parent": ("in", request_names)},
		)
		frappe.db.delete("HRP Master Data Request", {"name": ("in", request_names)})
	frappe.db.delete("HRP Master Data Domain", {"name": TEST_DOMAIN})
	frappe.db.delete(
		"HRP Service Idempotency",
		{"service_name": ("in", MASTER_DATA_SERVICE_NAMES)},
	)
	frappe.db.set_value(
		"Item",
		TEST_ITEM,
		{"item_name": "COD-020原始物料", "disabled": 0},
		update_modified=True,
	)
	reset_organization_state()


def create_domain(*, suffix: str = "base") -> dict[str, object]:
	return upsert_master_data_domain(
		build_master_data_domain_upsert(
			code=TEST_DOMAIN,
			display_name="COD-020物料主数据",
			target_doctype="Item",
			expected_revision=0,
		),
		idempotency_key=f"COD-020-domain-{suffix}",
		correlation_id=f"COD-020-domain-{suffix}",
	)


def create_context(*, suffix: str) -> tuple[dict[str, object], str]:
	from medops.tests.scope_fixtures import grant_test_scope, share_test_records

	version = create_version(suffix=f"cod020-{suffix}")
	grant_test_scope(
		code="COD020-TEST-SCOPE",
		company=TEST_COMPANY,
		hospital=TEST_HOSPITAL,
		users=(TEST_REQUESTER, TEST_REVIEWER),
	)
	share_test_records("Item", (TEST_ITEM,), (TEST_REQUESTER, TEST_REVIEWER))
	version_name = cast(str, version["name"])
	return version, unit_name(version_name, "OUTPATIENT")


def request_command(
	organization_unit: str,
	*,
	request_name: str | None = None,
	expected_revision: int = 0,
	operation: str = "Update",
	target_name: str | None = TEST_ITEM,
	proposed_value: str = "COD-020修订物料",
	field_name: str = "item_name",
):
	return build_master_data_request_upsert(
		request_name=request_name,
		master_data_domain=TEST_DOMAIN,
		company=TEST_COMPANY,
		hospital=TEST_HOSPITAL,
		organization_unit=organization_unit,
		operation=operation,
		target_name=target_name,
		subject="COD-020主数据变更",
		effective_on="2026-06-30",
		changes=[
			{
				"field_name": field_name,
				"proposed_value": proposed_value,
				"reason": "业务治理申请",
			}
		],
		expected_revision=expected_revision,
	)


class TestMasterDataGovernance(IntegrationTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_master_data_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_master_data_state()

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	def test_metadata_migration_workspace_and_service_only_permissions(self) -> None:
		domain_meta = frappe.get_meta("HRP Master Data Domain")
		request_meta = frappe.get_meta("HRP Master Data Request")
		change_meta = frappe.get_meta("HRP Master Data Change Item")
		self.assertEqual(domain_meta.get_field("target_doctype").label, "标准主数据类型")
		self.assertEqual(request_meta.get_field("changes").options, "HRP Master Data Change Item")
		self.assertEqual(change_meta.get_field("proposed_value").label, "建议值")
		self.assertTrue(request_meta.is_submittable)
		self.assertFalse(any(permission.write for permission in domain_meta.permissions))
		self.assertFalse(any(permission.write for permission in request_meta.permissions))
		self.assertEqual(ensure_master_data_governance()["schema_version"], 1)
		self.assertEqual(ensure_master_data_governance()["schema_version"], 1)
		workspace = frappe.get_doc("Workspace", "HRP Master Data")
		self.assertEqual(workspace.title, "主数据中心")
		sidebars = get_sidebar_items()
		self.assertIn("hrp", sidebars)
		self.assertEqual(sidebars["hrp"]["app"], "medops")
		self.assertEqual(
			[item["label"] for item in sidebars["hrp"]["items"][:3]],
			["首页", "组织管理", "主数据中心"],
		)
		default_workspace_map = build_default_workspace_map(sidebars)
		self.assertEqual(default_workspace_map["HRP Organization"], "HRP")
		self.assertEqual(default_workspace_map["hrp-master-data-access"], "HRP")

		direct = frappe.get_doc(
			{
				"doctype": "HRP Master Data Domain",
				"code": TEST_DOMAIN,
				"display_name": "非法直写",
				"target_doctype": "Item",
				"revision": 1,
			}
		)
		with self.assertRaises(IoneApplicationError) as raised:
			direct.insert(ignore_permissions=True)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")

	def test_domain_is_revisioned_idempotent_and_permissioned(self) -> None:
		command = build_master_data_domain_upsert(
			code=TEST_DOMAIN,
			display_name="COD-020物料主数据",
			target_doctype="Item",
			expected_revision=0,
		)
		first = upsert_master_data_domain(
			command,
			idempotency_key="COD-020-domain-upsert",
			correlation_id="COD-020-domain-upsert",
		)
		replay = upsert_master_data_domain(
			command,
			idempotency_key="COD-020-domain-upsert",
			correlation_id="COD-020-domain-replay",
		)
		self.assertEqual(first["revision"], 1)
		self.assertTrue(replay["idempotency_replayed"])
		updated = upsert_master_data_domain(
			build_master_data_domain_upsert(
				code=TEST_DOMAIN,
				display_name="COD-020物料治理",
				target_doctype="Item",
				expected_revision=1,
			),
			idempotency_key="COD-020-domain-update",
			correlation_id="COD-020-domain-update",
		)
		self.assertEqual(updated["revision"], 2)
		self.assertEqual(updated["changed_fields"], ["display_name"])
		before = frappe.db.count("HRP Service Idempotency")
		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			self.assertRaises(IoneApplicationError) as denied,
		):
			upsert_master_data_domain(
				build_master_data_domain_upsert(
					code=TEST_DOMAIN,
					display_name="拒绝",
					target_doctype="Item",
					expected_revision=2,
				),
				idempotency_key="COD-020-domain-denied",
			)
		self.assertEqual(denied.exception.code, "IONE-CORE-0002")
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)

	def test_draft_is_structured_revisioned_owned_and_service_only(self) -> None:
		create_domain(suffix="draft")
		_, organization_unit = create_context(suffix="draft")
		first = save_master_data_request(
			request_command(organization_unit),
			idempotency_key="COD-020-request-create",
			correlation_id="COD-020-request-create",
		)
		self.assertEqual(first["request_status"], "Draft")
		self.assertEqual(first["revision"], 1)
		first_changes = cast(list[dict[str, object]], first["changes"])
		self.assertEqual(first_changes[0]["current_value"], "COD-020原始物料")
		self.assertEqual(first_changes[0]["proposed_value"], "COD-020修订物料")
		replay = save_master_data_request(
			request_command(organization_unit),
			idempotency_key="COD-020-request-create",
			correlation_id="COD-020-request-replay",
		)
		self.assertTrue(replay["idempotency_replayed"])
		direct = frappe.get_doc("HRP Master Data Request", first["name"])
		direct.subject = "非法直改"
		with self.assertRaises(IoneApplicationError) as raised:
			direct.save(ignore_permissions=True)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")

	def test_desk_permissions_limit_hrp_users_to_their_own_requests(self) -> None:
		create_domain(suffix="desk-permission")
		_, organization_unit = create_context(suffix="desk-permission")
		admin_request = save_master_data_request(
			request_command(organization_unit),
			idempotency_key="COD-020-request-admin",
		)
		frappe.set_user(TEST_REQUESTER)
		own_request = save_master_data_request(
			request_command(organization_unit, proposed_value="COD-020申请人物料"),
			idempotency_key="COD-020-request-owner",
		)

		visible_names = frappe.get_list(
			"HRP Master Data Request",
			filters={"master_data_domain": TEST_DOMAIN},
			pluck="name",
		)
		self.assertEqual(visible_names, [])
		from medops.api.v1.master_data_access import list_records

		self.assertEqual(
			[row["name"] for row in cast(list[dict[str, object]], list_records(entity="requests")["items"])],
			[own_request["name"]],
		)
		self.assertTrue(
			frappe.has_permission(
				"HRP Master Data Request",
				ptype="read",
				doc=frappe.get_doc("HRP Master Data Request", own_request["name"]),
				user=TEST_REQUESTER,
			)
		)
		self.assertFalse(
			frappe.has_permission(
				"HRP Master Data Request",
				ptype="read",
				doc=frappe.get_doc("HRP Master Data Request", admin_request["name"]),
				user=TEST_REQUESTER,
			)
		)

	def test_submit_and_review_enforce_maker_checker_without_mutating_item(self) -> None:
		create_domain(suffix="review")
		_, organization_unit = create_context(suffix="review")
		draft = save_master_data_request(
			request_command(organization_unit),
			idempotency_key="COD-020-review-draft",
		)
		submitted = submit_master_data_request(
			build_master_data_request_submit(
				request_name=draft["name"],
				expected_revision=draft["revision"],
			),
			idempotency_key="COD-020-review-submit",
		)
		self.assertEqual(submitted["request_status"], "Pending Review")
		self.assertEqual(submitted["docstatus"], 1)
		with self.assertRaises(IoneApplicationError) as same_user:
			review_master_data_request(
				build_master_data_request_review(
					request_name=draft["name"],
					expected_revision=submitted["revision"],
					decision="Approve",
				),
				idempotency_key="COD-020-review-same-user",
			)
		self.assertEqual(same_user.exception.code, "IONE-CORE-0008")
		frappe.set_user(TEST_REVIEWER)
		approved = review_master_data_request(
			build_master_data_request_review(
				request_name=draft["name"],
				expected_revision=submitted["revision"],
				decision="Approve",
			),
			idempotency_key="COD-020-review-approve",
		)
		self.assertEqual(approved["request_status"], "Approved")
		self.assertEqual(approved["reviewed_by"], TEST_REVIEWER)
		self.assertEqual(
			frappe.db.get_value("Item", TEST_ITEM, "item_name"),
			"COD-020原始物料",
		)

	def test_target_drift_unknown_field_and_link_are_rejected(self) -> None:
		create_domain(suffix="drift")
		_, organization_unit = create_context(suffix="drift")
		draft = save_master_data_request(
			request_command(organization_unit),
			idempotency_key="COD-020-drift-draft",
		)
		frappe.db.set_value("Item", TEST_ITEM, "item_name", "外部并发修改")
		with self.assertRaises(IoneApplicationError) as conflict:
			submit_master_data_request(
				build_master_data_request_submit(
					request_name=draft["name"],
					expected_revision=draft["revision"],
				),
				idempotency_key="cod-drift",
			)
		self.assertEqual(conflict.exception.code, "IONE-CORE-0005")
		for field_name, value, expected_code in (
			("description", "不在白名单", "IONE-CORE-0008"),
			("item_group", "不存在物料组", "IONE-CORE-0004"),
		):
			with (
				self.subTest(field_name=field_name),
				self.assertRaises(IoneApplicationError) as raised,
			):
				save_master_data_request(
					request_command(
						organization_unit,
						field_name=field_name,
						proposed_value=value,
					),
					idempotency_key=f"COD-020-invalid-{field_name}",
				)
			self.assertEqual(raised.exception.code, expected_code)

	def test_request_read_scope_and_audit_redact_values(self) -> None:
		create_domain(suffix="redaction")
		_, organization_unit = create_context(suffix="redaction")
		sentinel = "COD-020不可进入审计的敏感建议值"
		with patch("medops.services.audit_context.frappe.logger") as logger:
			draft = save_master_data_request(
				request_command(organization_unit, proposed_value=sentinel),
				idempotency_key="COD-020-redaction-request",
				correlation_id="COD-020-redaction-request",
			)
		payload = json.dumps(
			[
				call.args[0]
				for level in (logger.return_value.info, logger.return_value.warning)
				for call in level.call_args_list
			],
			ensure_ascii=False,
		)
		self.assertNotIn(sentinel, payload)
		record = frappe.get_doc(
			"HRP Service Idempotency",
			idempotency_record_name(
				SaveMasterDataRequestService.definition.name,
				"COD-020-redaction-request",
			),
		)
		self.assertNotIn(sentinel, record.request_fingerprint)
		self.assertEqual(get_master_data_request(request_name=draft["name"])["name"], draft["name"])
		original_user = frappe.session.user
		try:
			frappe.session.user = "another@example.com"
			with (
				patch(
					"medops.hrp_master_data.services.master_data.frappe.get_roles",
					return_value=["HRP User"],
				),
				self.assertRaises(IoneApplicationError) as denied,
			):
				get_master_data_request(request_name=draft["name"])
		finally:
			frappe.session.user = original_user
		self.assertEqual(denied.exception.code, "IONE-CORE-0002")


class TestMasterDataGovernanceAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_master_data_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_master_data_state()
		frappe.local.db.commit()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def _upsert_domain_over_http(self, *, suffix: str) -> dict[str, object]:
		response = self.post(
			self.method(UPSERT_DOMAIN_METHOD),
			{
				"code": TEST_DOMAIN,
				"display_name": "COD-020 HTTP物料域",
				"target_doctype": "Item",
				"expected_revision": 0,
			},
			headers={"Idempotency-Key": f"COD-020-http-domain-{suffix}"},
		)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		return cast(dict[str, object], response.get_json()["message"])

	def _create_published_version_over_http(self, *, suffix: str) -> dict[str, object]:
		hospital_response = self.post(
			self.method(UPSERT_HOSPITAL_METHOD),
			{
				"code": TEST_HOSPITAL,
				"company": TEST_COMPANY,
				"display_name": "COD-020 HTTP测试医院",
				"expected_revision": 0,
			},
			headers={"Idempotency-Key": f"COD-020-http-hospital-{suffix}"},
		)
		self.assertEqual(
			hospital_response.status_code,
			200,
			hospital_response.get_data(as_text=True),
		)
		version_response = self.post(
			self.method(CREATE_VERSION_METHOD),
			{
				"hospital": TEST_HOSPITAL,
				"effective_from": "2026-01-01",
				"version_label": "COD-020 HTTP组织版本",
			},
			headers={"Idempotency-Key": f"COD-020-http-version-{suffix}"},
		)
		self.assertEqual(
			version_response.status_code,
			200,
			version_response.get_data(as_text=True),
		)
		version = version_response.get_json()["message"]
		hierarchy_response = self.post(
			self.method(REPLACE_HIERARCHY_METHOD),
			{
				"organization_version": version["name"],
				"expected_revision": version["revision"],
				"nodes": json.dumps(hierarchy_nodes(), ensure_ascii=False),
			},
			headers={"Idempotency-Key": f"COD-020-http-hierarchy-{suffix}"},
		)
		self.assertEqual(
			hierarchy_response.status_code,
			200,
			hierarchy_response.get_data(as_text=True),
		)
		replaced = hierarchy_response.get_json()["message"]
		publish_response = self.post(
			self.method(PUBLISH_VERSION_METHOD),
			{
				"organization_version": version["name"],
				"expected_revision": replaced["revision"],
			},
			headers={"Idempotency-Key": f"COD-020-http-publish-{suffix}"},
		)
		self.assertEqual(
			publish_response.status_code,
			200,
			publish_response.get_data(as_text=True),
		)
		return cast(dict[str, object], publish_response.get_json()["message"])

	def _save_request_over_http(
		self,
		*,
		organization_unit: str,
		suffix: str,
	) -> dict[str, object]:
		response = self.post(
			self.method(SAVE_REQUEST_METHOD),
			{
				"master_data_domain": TEST_DOMAIN,
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"organization_unit": organization_unit,
				"operation": "Update",
				"target_name": TEST_ITEM,
				"subject": "COD-020 HTTP申请",
				"effective_on": "2026-06-30",
				"changes": json.dumps(
					[{"field_name": "item_name", "proposed_value": "HTTP修订物料"}],
					ensure_ascii=False,
				),
			},
			headers={"Idempotency-Key": f"COD-020-http-draft-{suffix}"},
		)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		return cast(dict[str, object], response.get_json()["message"])

	def _submit_request_over_http(
		self,
		*,
		draft: dict[str, object],
		suffix: str,
	) -> dict[str, object]:
		response = self.post(
			self.method(SUBMIT_REQUEST_METHOD),
			{
				"request_name": draft["name"],
				"expected_revision": draft["revision"],
			},
			headers={"Idempotency-Key": f"COD-020-http-submit-{suffix}"},
		)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		return cast(dict[str, object], response.get_json()["message"])

	def test_http_domain_draft_submit_and_query(self) -> None:
		self._upsert_domain_over_http(suffix="lifecycle")
		version = self._create_published_version_over_http(suffix="lifecycle")
		organization_unit = f"{version['name']}-OUTPATIENT"
		draft = self._save_request_over_http(
			organization_unit=organization_unit,
			suffix="lifecycle",
		)
		submitted = self._submit_request_over_http(draft=draft, suffix="lifecycle")
		query_response = self.get(
			self.method(GET_REQUEST_METHOD),
			{"request_name": draft["name"]},
		)
		self.assertEqual(query_response.status_code, 200, query_response.get_data(as_text=True))
		self.assertEqual(query_response.get_json()["message"]["request_status"], "Pending Review")
		self.assertEqual(submitted["request_status"], "Pending Review")

	def test_http_write_requires_idempotency_header_and_review_is_maker_checked(self) -> None:
		response = self.post(
			self.method(UPSERT_DOMAIN_METHOD),
			{
				"code": TEST_DOMAIN,
				"display_name": "COD-020 HTTP物料域",
				"target_doctype": "Item",
				"expected_revision": 0,
			},
		)
		self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
		self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0003")
		self._upsert_domain_over_http(suffix="review")
		version = self._create_published_version_over_http(suffix="review")
		draft = self._save_request_over_http(
			organization_unit=f"{version['name']}-OUTPATIENT",
			suffix="review",
		)
		submitted = self._submit_request_over_http(draft=draft, suffix="review")
		review_response = self.post(
			self.method(REVIEW_REQUEST_METHOD),
			{
				"request_name": draft["name"],
				"expected_revision": submitted["revision"],
				"decision": "Approve",
			},
			headers={"Idempotency-Key": "COD-020-http-review-same-user"},
		)
		self.assertEqual(
			review_response.status_code,
			403,
			review_response.get_data(as_text=True),
		)
		self.assertEqual(review_response.headers["X-Ione-Error-Code"], "IONE-CORE-0008")
