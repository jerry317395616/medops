from __future__ import annotations

import json
from typing import cast
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.domain_service import idempotency_record_name
from medops.common.error_catalog import IoneApplicationError
from medops.common.organization import (
	HierarchyReplace,
	HospitalUpsert,
	OrganizationVersionCreate,
	OrganizationVersionPublish,
	build_hierarchy_replace,
	build_hospital_upsert,
	build_organization_version_create,
	build_organization_version_publish,
)
from medops.hrp_organization.services.organization import (
	CreateOrganizationVersionService,
	PublishOrganizationVersionService,
	ReplaceOrganizationHierarchyService,
	UpsertHospitalService,
	create_organization_version,
	get_organization_hierarchy,
	publish_organization_version,
	replace_organization_hierarchy,
	upsert_hospital,
)
from medops.setup.organization import ensure_organization_hierarchy

UPSERT_HOSPITAL_METHOD = "medops.api.v1.organization.upsert_hospital"
CREATE_VERSION_METHOD = "medops.api.v1.organization.create_organization_version"
REPLACE_HIERARCHY_METHOD = "medops.api.v1.organization.replace_organization_hierarchy"
PUBLISH_VERSION_METHOD = "medops.api.v1.organization.publish_organization_version"
GET_HIERARCHY_METHOD = "medops.api.v1.organization.get_organization_hierarchy"
TEST_HOSPITAL = "COD018-HOSPITAL"
TEST_COMPANY = "COD-018测试医疗法人"
TEST_WAREHOUSE_TYPE = "Transit"
ORGANIZATION_SERVICE_NAMES = (
	UpsertHospitalService.definition.name,
	CreateOrganizationVersionService.definition.name,
	ReplaceOrganizationHierarchyService.definition.name,
	PublishOrganizationVersionService.definition.name,
)


def ensure_company_fixture() -> None:
	if frappe.db.exists("Company", TEST_COMPANY):
		return
	if not frappe.db.exists("Warehouse Type", TEST_WAREHOUSE_TYPE):
		frappe.get_doc(
			{
				"doctype": "Warehouse Type",
				"name": TEST_WAREHOUSE_TYPE,
				"description": "COD-018测试中转仓类型",
			}
		).insert(ignore_permissions=True)
	company = frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": TEST_COMPANY,
			"abbr": "C018",
			"country": "China",
			"default_currency": "CNY",
		}
	)
	company.insert(ignore_permissions=True)
	frappe.local.db.commit()


def company_name() -> str:
	if not frappe.db.exists("Company", TEST_COMPANY):
		raise RuntimeError("COD-018 company fixture is not initialized")
	return TEST_COMPANY


def reset_organization_test_state() -> None:
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


def hospital_command(
	*,
	expected_revision: int = 0,
	display_name: str = "COD-018测试医院",
	company: str | None = None,
) -> HospitalUpsert:
	return build_hospital_upsert(
		code=TEST_HOSPITAL,
		company=company or company_name(),
		display_name=display_name,
		enabled=True,
		valid_from="2026-01-01",
		valid_to=None,
		remarks="组织测试",
		expected_revision=expected_revision,
	)


def execute_hospital(
	command: HospitalUpsert,
	deduplication_id: object | None,
	correlation_id: object | None,
) -> dict[str, object]:
	return upsert_hospital(
		command,
		idempotency_key=deduplication_id,
		correlation_id=correlation_id,
	)


def version_command(
	effective_from: str = "2026-01-01",
	label: str = "2026年组织版本",
) -> OrganizationVersionCreate:
	return build_organization_version_create(
		hospital=TEST_HOSPITAL,
		effective_from=effective_from,
		version_label=label,
		remarks="版本测试",
	)


def execute_version(
	command: OrganizationVersionCreate,
	deduplication_id: object | None,
	correlation_id: object | None,
) -> dict[str, object]:
	return create_organization_version(
		command,
		idempotency_key=deduplication_id,
		correlation_id=correlation_id,
	)


def hierarchy_nodes(
	*,
	root_name: str = "COD-018测试医院",
	department_name: str = "门诊部",
) -> list[dict[str, object]]:
	return [
		{
			"code": TEST_HOSPITAL,
			"display_name": root_name,
			"unit_type": "HOSPITAL",
			"parent_code": None,
			"is_group": 1,
			"enabled": 1,
			"sequence": 1,
			"valid_from": "2026-01-01",
		},
		{
			"code": "OUTPATIENT",
			"display_name": department_name,
			"unit_type": "CLINICAL_DEPARTMENT",
			"parent_code": TEST_HOSPITAL,
			"is_group": 1,
			"enabled": 1,
			"sequence": 1,
			"valid_from": "2026-01-01",
		},
		{
			"code": "CARDIOLOGY",
			"display_name": "心内科",
			"unit_type": "CLINICAL_DEPARTMENT",
			"parent_code": "OUTPATIENT",
			"is_group": 0,
			"enabled": 1,
			"sequence": 1,
			"valid_from": "2026-01-01",
		},
	]


def hierarchy_command(
	version: str,
	revision: int,
	*,
	nodes: list[dict[str, object]] | None = None,
) -> HierarchyReplace:
	return build_hierarchy_replace(
		organization_version=version,
		expected_revision=revision,
		nodes=nodes or hierarchy_nodes(),
	)


def execute_hierarchy(
	command: HierarchyReplace,
	deduplication_id: object | None,
	correlation_id: object | None,
) -> dict[str, object]:
	return replace_organization_hierarchy(
		command,
		idempotency_key=deduplication_id,
		correlation_id=correlation_id,
	)


def publish_command(version: str, revision: int) -> OrganizationVersionPublish:
	return build_organization_version_publish(
		organization_version=version,
		expected_revision=revision,
	)


def execute_publish(
	command: OrganizationVersionPublish,
	deduplication_id: object | None,
	correlation_id: object | None,
) -> dict[str, object]:
	return publish_organization_version(
		command,
		idempotency_key=deduplication_id,
		correlation_id=correlation_id,
	)


def create_hospital_and_version(
	*,
	effective_from: str = "2026-01-01",
	suffix: str = "base",
) -> dict[str, object]:
	if not frappe.db.exists("HRP Hospital", TEST_HOSPITAL):
		execute_hospital(
			hospital_command(),
			f"COD-018-hospital-{suffix}",
			f"COD-018-hospital-{suffix}",
		)
	return execute_version(
		version_command(effective_from),
		f"COD-018-version-{suffix}",
		f"COD-018-version-{suffix}",
	)


class TestOrganizationHierarchy(IntegrationTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_company_fixture()

	def setUp(self) -> None:
		super().setUp()
		reset_organization_test_state()

	def test_metadata_is_chinese_versioned_tree_and_service_governed(self) -> None:
		hospital_meta = frappe.get_meta("HRP Hospital")
		version_meta = frappe.get_meta("HRP Organization Version")
		unit_meta = frappe.get_meta("HRP Organization Unit")
		settings_meta = frappe.get_meta("HRP System Settings")
		self.assertEqual(hospital_meta.get_field("display_name").label, "医院名称")
		self.assertTrue(version_meta.is_submittable)
		self.assertEqual(version_meta.get_field("hierarchy_digest").read_only, 1)
		self.assertTrue(unit_meta.is_tree)
		self.assertEqual(unit_meta.nsm_parent_field, "parent_organization_unit")
		self.assertEqual(settings_meta.get_field("default_hospital").fieldtype, "Link")
		self.assertEqual(
			settings_meta.get_field("default_hospital").options,
			"HRP Hospital",
		)
		expected_roles = {"System Manager", "HRP System Manager", "HRP Data Steward"}
		for meta in (hospital_meta, version_meta, unit_meta):
			self.assertSetEqual({permission.role for permission in meta.permissions}, expected_roles)
			self.assertFalse(any(permission.delete for permission in meta.permissions))
		self.assertTrue(any(permission.write for permission in hospital_meta.permissions))
		self.assertFalse(any(permission.write for permission in version_meta.permissions))
		self.assertFalse(any(permission.write for permission in unit_meta.permissions))

	def test_legacy_default_hospital_is_migrated_without_losing_label(self) -> None:
		company = company_name()
		settings = frappe.get_single("HRP System Settings")
		settings.default_company = company
		settings.default_hospital = None
		settings.flags.system_settings_repair = True
		settings.save(ignore_permissions=True)
		frappe.db.set_single_value(
			"HRP System Settings",
			"default_hospital",
			"旧版默认医院名称",
		)
		first = ensure_organization_hierarchy()
		second = ensure_organization_hierarchy()
		migrated = frappe.get_single("HRP System Settings")
		self.assertTrue(first["default_hospital_changed"])
		self.assertFalse(second["default_hospital_changed"])
		self.assertTrue(frappe.db.exists("HRP Hospital", migrated.default_hospital))
		hospital = frappe.get_doc("HRP Hospital", migrated.default_hospital)
		self.assertEqual(hospital.display_name, "旧版默认医院名称")
		self.assertEqual(hospital.company, company)

	def test_hospital_upsert_is_versioned_idempotent_and_noop_stable(self) -> None:
		first = execute_hospital(
			hospital_command(),
			"COD-018-upsert-hospital",
			"COD-018-upsert-first",
		)
		replay = execute_hospital(
			hospital_command(),
			"COD-018-upsert-hospital",
			"COD-018-upsert-replay",
		)
		self.assertEqual(first["revision"], 1)
		self.assertFalse(first["idempotency_replayed"])
		self.assertTrue(replay["idempotency_replayed"])
		update = execute_hospital(
			hospital_command(expected_revision=1, display_name="COD-018中心医院"),
			"COD-018-update-hospital",
			"COD-018-update",
		)
		self.assertEqual(update["revision"], 2)
		self.assertEqual(update["changed_fields"], ["display_name"])
		noop = execute_hospital(
			hospital_command(expected_revision=2, display_name="COD-018中心医院"),
			"COD-018-noop-hospital",
			"COD-018-noop",
		)
		self.assertFalse(noop["changed"])
		self.assertEqual(noop["revision"], 2)

	def test_invalid_company_and_permission_fail_before_reservation(self) -> None:
		before = frappe.db.count("HRP Service Idempotency")
		with self.assertRaises(IoneApplicationError) as missing:
			execute_hospital(
				hospital_command(company="COD-018不存在法人"),
				"COD-018-invalid-company",
				"COD-018-invalid-company",
			)
		self.assertEqual(missing.exception.code, "IONE-CORE-0004")
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)
		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			self.assertRaises(IoneApplicationError) as denied,
		):
			execute_hospital(
				hospital_command(),
				"COD-018-denied-hospital",
				"COD-018-denied",
			)
		self.assertEqual(denied.exception.code, "IONE-CORE-0002")
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)

	def test_versions_allocate_monotonic_hospital_sequence(self) -> None:
		execute_hospital(
			hospital_command(),
			"COD-018-sequence-hospital",
			"COD-018-sequence-hospital",
		)
		first = execute_version(
			version_command("2026-01-01"),
			"COD-018-sequence-v1",
			"COD-018-sequence-v1",
		)
		second = execute_version(
			version_command("2026-07-01"),
			"COD-018-sequence-v2",
			"COD-018-sequence-v2",
		)
		self.assertEqual(first["version_number"], 1)
		self.assertEqual(first["name"], f"{TEST_HOSPITAL}-V0001")
		self.assertEqual(second["version_number"], 2)
		self.assertEqual(second["name"], f"{TEST_HOSPITAL}-V0002")
		self.assertEqual(
			frappe.db.get_value("HRP Hospital", TEST_HOSPITAL, "next_version_number"),
			3,
		)

	def test_replace_hierarchy_is_atomic_versioned_and_queryable(self) -> None:
		version = create_hospital_and_version(suffix="replace")
		result = execute_hierarchy(
			hierarchy_command(cast(str, version["name"]), cast(int, version["revision"])),
			"COD-018-replace-tree",
			"COD-018-replace-tree",
		)
		self.assertTrue(result["changed"])
		self.assertEqual(result["revision"], 2)
		self.assertEqual(result["node_count"], 3)
		self.assertEqual(
			frappe.db.count(
				"HRP Organization Unit",
				{"organization_version": version["name"]},
			),
			3,
		)
		hierarchy = get_organization_hierarchy(organization_version=cast(str, version["name"]))
		nodes = cast(list[dict[str, object]], hierarchy["nodes"])
		self.assertEqual([node["depth"] for node in nodes], [0, 1, 2])
		self.assertEqual(
			[node["code"] for node in nodes],
			[TEST_HOSPITAL, "OUTPATIENT", "CARDIOLOGY"],
		)

	def test_hierarchy_noop_and_idempotency_replay_keep_revision(self) -> None:
		version = create_hospital_and_version(suffix="noop")
		command = hierarchy_command(
			cast(str, version["name"]),
			cast(int, version["revision"]),
		)
		first = execute_hierarchy(command, "COD-018-tree-replay", "COD-018-tree-first")
		replay = execute_hierarchy(command, "COD-018-tree-replay", "COD-018-tree-replay")
		self.assertTrue(replay["idempotency_replayed"])
		self.assertEqual(replay["revision"], first["revision"])
		noop = execute_hierarchy(
			hierarchy_command(cast(str, version["name"]), cast(int, first["revision"])),
			"COD-018-tree-noop",
			"COD-018-tree-noop",
		)
		self.assertFalse(noop["changed"])
		self.assertEqual(noop["revision"], first["revision"])

	def test_stale_hierarchy_revision_rolls_back_reservation_and_nodes(self) -> None:
		version = create_hospital_and_version(suffix="stale")
		before = frappe.db.count("HRP Service Idempotency")
		with self.assertRaises(IoneApplicationError) as raised:
			execute_hierarchy(
				hierarchy_command(
					cast(str, version["name"]),
					cast(int, version["revision"]) + 1,
				),
				"COD-018-tree-stale",
				"COD-018-tree-stale",
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0005")
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)
		self.assertEqual(
			frappe.db.count(
				"HRP Organization Unit",
				{"organization_version": version["name"]},
			),
			0,
		)

	def test_published_version_and_units_are_immutable(self) -> None:
		version = create_hospital_and_version(suffix="publish")
		replaced = execute_hierarchy(
			hierarchy_command(cast(str, version["name"]), cast(int, version["revision"])),
			"COD-018-publish-tree",
			"COD-018-publish-tree",
		)
		published = execute_publish(
			publish_command(cast(str, version["name"]), cast(int, replaced["revision"])),
			"COD-018-publish-version",
			"COD-018-publish-version",
		)
		self.assertEqual(published["status"], "Published")
		self.assertEqual(published["docstatus"], 1)
		unit_name = frappe.db.get_value(
			"HRP Organization Unit",
			{"organization_version": version["name"], "code": "CARDIOLOGY"},
			"name",
		)
		unit = frappe.get_doc("HRP Organization Unit", unit_name)
		unit.display_name = "不允许修改"
		with self.assertRaises(IoneApplicationError) as unit_error:
			unit.save(ignore_permissions=True)
		self.assertEqual(unit_error.exception.code, "IONE-CORE-0008")
		version_doc = frappe.get_doc("HRP Organization Version", version["name"])
		version_doc.version_label = "不允许修改"
		with self.assertRaises(IoneApplicationError) as version_error:
			version_doc.save(ignore_permissions=True)
		self.assertEqual(version_error.exception.code, "IONE-CORE-0008")

	def test_effective_date_selects_latest_published_snapshot(self) -> None:
		execute_hospital(
			hospital_command(),
			"COD-018-asof-hospital",
			"COD-018-asof-hospital",
		)
		first = execute_version(
			version_command("2026-01-01", "上半年"),
			"COD-018-asof-v1",
			"COD-018-asof-v1",
		)
		first_tree = execute_hierarchy(
			hierarchy_command(
				cast(str, first["name"]),
				cast(int, first["revision"]),
				nodes=hierarchy_nodes(department_name="上半年门诊部"),
			),
			"COD-018-asof-tree1",
			"COD-018-asof-tree1",
		)
		execute_publish(
			publish_command(cast(str, first["name"]), cast(int, first_tree["revision"])),
			"COD-018-asof-publish1",
			"COD-018-asof-publish1",
		)
		second = execute_version(
			version_command("2026-07-01", "下半年"),
			"COD-018-asof-v2",
			"COD-018-asof-v2",
		)
		second_nodes = hierarchy_nodes(department_name="下半年门诊部")
		for node in second_nodes:
			node["valid_from"] = "2026-07-01"
		second_tree = execute_hierarchy(
			hierarchy_command(
				cast(str, second["name"]),
				cast(int, second["revision"]),
				nodes=second_nodes,
			),
			"COD-018-asof-tree2",
			"COD-018-asof-tree2",
		)
		execute_publish(
			publish_command(cast(str, second["name"]), cast(int, second_tree["revision"])),
			"COD-018-asof-publish2",
			"COD-018-asof-publish2",
		)
		june = get_organization_hierarchy(
			hospital=TEST_HOSPITAL,
			effective_on="2026-06-30",
		)
		july = get_organization_hierarchy(
			hospital=TEST_HOSPITAL,
			effective_on="2026-07-01",
		)
		june_version = cast(dict[str, object], june["version"])
		july_version = cast(dict[str, object], july["version"])
		july_nodes = cast(list[dict[str, object]], july["nodes"])
		self.assertEqual(june_version["name"], first["name"])
		self.assertEqual(july_version["name"], second["name"])
		self.assertEqual(july_nodes[1]["display_name"], "下半年门诊部")

	def test_audit_and_idempotency_do_not_store_names_or_remarks(self) -> None:
		sentinel = "COD-018敏感医院说明"
		command = hospital_command(display_name=sentinel)
		with patch("medops.services.audit_context.frappe.logger") as logger:
			execute_hospital(command, "COD-018-redaction", "COD-018-redaction")
		audit_payload = json.dumps(
			[
				call.args[0]
				for level in (logger.return_value.info, logger.return_value.warning)
				for call in level.call_args_list
			],
			ensure_ascii=False,
		)
		self.assertNotIn(sentinel, audit_payload)
		record = frappe.get_doc(
			"HRP Service Idempotency",
			idempotency_record_name(
				UpsertHospitalService.definition.name,
				"COD-018-redaction",
			),
		)
		self.assertNotIn(sentinel, record.as_json())

	def test_same_request_key_with_different_hierarchy_conflicts(self) -> None:
		version = create_hospital_and_version(suffix="conflict")
		first = hierarchy_command(
			cast(str, version["name"]),
			cast(int, version["revision"]),
		)
		execute_hierarchy(first, "COD-018-tree-conflict", "COD-018-tree-conflict1")
		changed_nodes = hierarchy_nodes(department_name="另一个门诊部")
		with self.assertRaises(IoneApplicationError) as raised:
			execute_hierarchy(
				hierarchy_command(
					cast(str, version["name"]),
					cast(int, version["revision"]),
					nodes=changed_nodes,
				),
				"COD-018-tree-conflict",
				"COD-018-tree-conflict2",
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0007")


class TestOrganizationHierarchyAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_company_fixture()

	def setUp(self) -> None:
		super().setUp()
		reset_organization_test_state()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def test_http_full_lifecycle_and_effective_query(self) -> None:
		company = company_name()
		headers = {
			"Idempotency-Key": "COD-018-http-hospital",
			"X-Correlation-ID": "COD-018-http-hospital",
		}
		hospital_response = self.post(
			self.method(UPSERT_HOSPITAL_METHOD),
			{
				"code": TEST_HOSPITAL,
				"company": company,
				"display_name": "HTTP测试医院",
				"expected_revision": 0,
			},
			headers=headers,
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
				"version_label": "HTTP组织版本",
			},
			headers={
				"Idempotency-Key": "COD-018-http-version",
				"X-Correlation-ID": "COD-018-http-version",
			},
		)
		self.assertEqual(
			version_response.status_code,
			200,
			version_response.get_data(as_text=True),
		)
		version = version_response.get_json()["message"]
		replace_response = self.post(
			self.method(REPLACE_HIERARCHY_METHOD),
			{
				"organization_version": version["name"],
				"expected_revision": version["revision"],
				"nodes": json.dumps(hierarchy_nodes(), ensure_ascii=False),
			},
			headers={
				"Idempotency-Key": "COD-018-http-hierarchy",
				"X-Correlation-ID": "COD-018-http-hierarchy",
			},
		)
		self.assertEqual(
			replace_response.status_code,
			200,
			replace_response.get_data(as_text=True),
		)
		replaced = replace_response.get_json()["message"]
		publish_response = self.post(
			self.method(PUBLISH_VERSION_METHOD),
			{
				"organization_version": version["name"],
				"expected_revision": replaced["revision"],
			},
			headers={
				"Idempotency-Key": "COD-018-http-publish",
				"X-Correlation-ID": "COD-018-http-publish",
			},
		)
		self.assertEqual(
			publish_response.status_code,
			200,
			publish_response.get_data(as_text=True),
		)
		query = self.get(
			self.method(GET_HIERARCHY_METHOD),
			{"hospital": TEST_HOSPITAL, "effective_on": "2026-01-01"},
		)
		self.assertEqual(query.status_code, 200, query.get_data(as_text=True))
		payload = query.get_json()["message"]
		self.assertEqual(payload["version"]["status"], "Published")
		self.assertEqual(len(payload["nodes"]), 3)
		self.assertTrue(query.headers["X-Correlation-ID"])

	def test_http_write_requires_idempotency_header(self) -> None:
		response = self.post(
			self.method(UPSERT_HOSPITAL_METHOD),
			{
				"code": TEST_HOSPITAL,
				"company": company_name(),
				"display_name": "缺少幂等键",
				"expected_revision": 0,
			},
			headers={"X-Correlation-ID": "COD-018-http-missing"},
		)
		self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
		self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0003")
