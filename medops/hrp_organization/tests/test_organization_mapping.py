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
	build_hierarchy_replace,
	build_hospital_upsert,
	build_organization_version_create,
	build_organization_version_publish,
)
from medops.common.organization_mapping import (
	build_organization_mapping_resolve,
	build_organization_mapping_upsert,
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
from medops.hrp_organization.services.organization_mapping import (
	UpsertOrganizationMappingService,
	resolve_organization_mapping,
	upsert_organization_mapping,
)
from medops.setup.organization import ensure_organization_hierarchy

UPSERT_HOSPITAL_METHOD = "medops.api.v1.organization.upsert_hospital"
CREATE_VERSION_METHOD = "medops.api.v1.organization.create_organization_version"
REPLACE_HIERARCHY_METHOD = "medops.api.v1.organization.replace_organization_hierarchy"
PUBLISH_VERSION_METHOD = "medops.api.v1.organization.publish_organization_version"
UPSERT_MAPPING_METHOD = "medops.api.v1.organization_mapping.upsert_organization_mapping"
RESOLVE_MAPPING_METHOD = "medops.api.v1.organization_mapping.resolve_organization_mapping"

TEST_COMPANY = "COD-019测试医疗法人"
TEST_COMPANY_ABBR = "C019"
TEST_HOSPITAL = "COD019-HOSPITAL"
TEST_WAREHOUSE_TYPE = "Transit"
TEST_DEPARTMENT_PARENT = "COD019门诊部"
TEST_DEPARTMENT_CHILD = "COD019心内科"
TEST_DEPARTMENT_UNRELATED = "COD019财务部"
TEST_COST_CENTER_PARENT = "COD019门诊部"
TEST_COST_CENTER_CHILD = "COD019心内科"
TEST_COST_CENTER_UNRELATED = "COD019财务部"
MAPPING_SERVICE_NAMES = (
	UpsertHospitalService.definition.name,
	CreateOrganizationVersionService.definition.name,
	ReplaceOrganizationHierarchyService.definition.name,
	PublishOrganizationVersionService.definition.name,
	UpsertOrganizationMappingService.definition.name,
)


def ensure_company_fixture() -> None:
	if not frappe.db.exists("Warehouse Type", TEST_WAREHOUSE_TYPE):
		frappe.get_doc(
			{
				"doctype": "Warehouse Type",
				"name": TEST_WAREHOUSE_TYPE,
				"description": "COD-019测试中转仓类型",
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
	_ensure_department_fixture()
	_ensure_cost_center_fixture()
	frappe.local.db.commit()


def _ensure_department_fixture() -> None:
	root = frappe.db.get_value(
		"Department",
		{"parent_department": ("is", "not set"), "is_group": 1},
		"name",
	)
	if not root:
		raise RuntimeError("ERPNext Department root is missing")
	parent = _department_by_label(TEST_DEPARTMENT_PARENT)
	if not parent:
		parent_doc = frappe.get_doc(
			{
				"doctype": "Department",
				"department_name": TEST_DEPARTMENT_PARENT,
				"company": TEST_COMPANY,
				"parent_department": root,
				"is_group": 1,
			}
		)
		parent_doc.insert(ignore_permissions=True)
		parent = parent_doc.name
	for label in (TEST_DEPARTMENT_CHILD, TEST_DEPARTMENT_UNRELATED):
		if _department_by_label(label):
			continue
		frappe.get_doc(
			{
				"doctype": "Department",
				"department_name": label,
				"company": TEST_COMPANY,
				"parent_department": parent if label == TEST_DEPARTMENT_CHILD else root,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)


def _ensure_cost_center_fixture() -> None:
	root = frappe.db.get_value(
		"Cost Center",
		{
			"company": TEST_COMPANY,
			"parent_cost_center": ("is", "not set"),
			"is_group": 1,
		},
		"name",
	)
	if not root:
		root_doc = frappe.get_doc(
			{
				"doctype": "Cost Center",
				"cost_center_name": TEST_COMPANY,
				"company": TEST_COMPANY,
				"is_group": 1,
			}
		)
		root_doc.insert(ignore_permissions=True)
		root = root_doc.name
	parent = _cost_center_by_label(TEST_COST_CENTER_PARENT)
	if not parent:
		parent_doc = frappe.get_doc(
			{
				"doctype": "Cost Center",
				"cost_center_name": TEST_COST_CENTER_PARENT,
				"company": TEST_COMPANY,
				"parent_cost_center": root,
				"is_group": 1,
			}
		)
		parent_doc.insert(ignore_permissions=True)
		parent = parent_doc.name
	for label in (TEST_COST_CENTER_CHILD, TEST_COST_CENTER_UNRELATED):
		if _cost_center_by_label(label):
			continue
		frappe.get_doc(
			{
				"doctype": "Cost Center",
				"cost_center_name": label,
				"company": TEST_COMPANY,
				"parent_cost_center": parent if label == TEST_COST_CENTER_CHILD else root,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)


def _department_by_label(label: str) -> str | None:
	value = frappe.db.get_value(
		"Department",
		{"department_name": label, "company": TEST_COMPANY},
		"name",
	)
	return str(value) if value else None


def _cost_center_by_label(label: str) -> str | None:
	value = frappe.db.get_value(
		"Cost Center",
		{"cost_center_name": label, "company": TEST_COMPANY},
		"name",
	)
	return str(value) if value else None


def standard_targets(kind: str = "parent") -> tuple[str, str]:
	labels = {
		"parent": (TEST_DEPARTMENT_PARENT, TEST_COST_CENTER_PARENT),
		"child": (TEST_DEPARTMENT_CHILD, TEST_COST_CENTER_CHILD),
		"unrelated": (TEST_DEPARTMENT_UNRELATED, TEST_COST_CENTER_UNRELATED),
	}
	department_label, cost_center_label = labels[kind]
	department = _department_by_label(department_label)
	cost_center = _cost_center_by_label(cost_center_label)
	if not department or not cost_center:
		raise RuntimeError("COD-019 standard mapping fixtures are missing")
	return department, cost_center


def reset_mapping_test_state() -> None:
	version_names = frappe.get_all(
		"HRP Organization Version",
		filters={"hospital": TEST_HOSPITAL},
		pluck="name",
	)
	if version_names:
		frappe.db.delete(
			"HRP Organization Mapping",
			{"organization_version": ("in", version_names)},
		)
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
		{"service_name": ("in", MAPPING_SERVICE_NAMES)},
	)
	for doctype, label in (
		("Department", TEST_DEPARTMENT_PARENT),
		("Department", TEST_DEPARTMENT_CHILD),
		("Department", TEST_DEPARTMENT_UNRELATED),
		("Cost Center", TEST_COST_CENTER_PARENT),
		("Cost Center", TEST_COST_CENTER_CHILD),
		("Cost Center", TEST_COST_CENTER_UNRELATED),
	):
		name = _department_by_label(label) if doctype == "Department" else _cost_center_by_label(label)
		if name and frappe.db.get_value(doctype, name, "disabled"):
			frappe.db.set_value(doctype, name, "disabled", 0, update_modified=False)


def hierarchy_nodes() -> list[dict[str, object]]:
	return [
		{
			"code": TEST_HOSPITAL,
			"display_name": "COD-019测试医院",
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


def create_version(*, publish: bool = True, suffix: str = "base") -> dict[str, object]:
	hospital = build_hospital_upsert(
		code=TEST_HOSPITAL,
		company=TEST_COMPANY,
		display_name="COD-019测试医院",
		expected_revision=0,
	)
	upsert_hospital(
		hospital,
		idempotency_key=f"COD-019-hospital-{suffix}",
		correlation_id=f"COD-019-hospital-{suffix}",
	)
	version = create_organization_version(
		build_organization_version_create(
			hospital=TEST_HOSPITAL,
			effective_from="2026-01-01",
			version_label="COD-019组织版本",
		),
		idempotency_key=f"COD-019-version-{suffix}",
		correlation_id=f"COD-019-version-{suffix}",
	)
	replaced = replace_organization_hierarchy(
		build_hierarchy_replace(
			organization_version=version["name"],
			expected_revision=version["revision"],
			nodes=hierarchy_nodes(),
		),
		idempotency_key=f"COD-019-hierarchy-{suffix}",
		correlation_id=f"COD-019-hierarchy-{suffix}",
	)
	if not publish:
		return replaced
	return publish_organization_version(
		build_organization_version_publish(
			organization_version=version["name"],
			expected_revision=replaced["revision"],
		),
		idempotency_key=f"COD-019-publish-{suffix}",
		correlation_id=f"COD-019-publish-{suffix}",
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


def mapping_command(
	version: str,
	code: str,
	*,
	target_kind: str = "parent",
	expected_revision: int = 0,
	enabled: bool = True,
	remarks: str | None = None,
):
	department, cost_center = standard_targets(target_kind)
	return build_organization_mapping_upsert(
		organization_version=version,
		organization_unit=unit_name(version, code),
		department=department,
		cost_center=cost_center,
		enabled=enabled,
		expected_revision=expected_revision,
		remarks=remarks,
	)


class TestOrganizationMapping(IntegrationTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_company_fixture()

	def setUp(self) -> None:
		super().setUp()
		reset_mapping_test_state()

	def test_metadata_migration_and_service_only_permissions(self) -> None:
		meta = frappe.get_meta("HRP Organization Mapping")
		self.assertEqual(meta.module, "HRP Organization")
		self.assertEqual(meta.get_field("department").label, "标准部门")
		self.assertEqual(meta.get_field("department").options, "Department")
		self.assertEqual(meta.get_field("cost_center").options, "Cost Center")
		self.assertEqual(meta.get_field("revision").read_only, 1)
		self.assertSetEqual(
			{permission.role for permission in meta.permissions},
			{
				"System Manager",
				"HRP System Manager",
				"HRP Data Steward",
				"HRP Integration User",
			},
		)
		self.assertFalse(any(permission.write for permission in meta.permissions))
		self.assertEqual(ensure_organization_hierarchy()["schema_version"], 2)
		self.assertEqual(ensure_organization_hierarchy()["schema_version"], 2)

		version = create_version(suffix="direct")
		department, cost_center = standard_targets()
		doc = frappe.get_doc(
			{
				"doctype": "HRP Organization Mapping",
				"organization_version": version["name"],
				"organization_unit": unit_name(cast(str, version["name"]), "OUTPATIENT"),
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"unit_code": "OUTPATIENT",
				"unit_type": "CLINICAL_DEPARTMENT",
				"department": department,
				"cost_center": cost_center,
				"enabled": 1,
				"revision": 1,
			}
		)
		with self.assertRaises(IoneApplicationError) as raised:
			doc.insert(ignore_permissions=True)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")

	def test_upsert_is_revisioned_idempotent_and_noop_stable(self) -> None:
		version = create_version(suffix="upsert")
		version_name = cast(str, version["name"])
		command = mapping_command(version_name, "OUTPATIENT")
		first = upsert_organization_mapping(
			command,
			idempotency_key="COD-019-map-upsert",
			correlation_id="COD-019-map-first",
		)
		replay = upsert_organization_mapping(
			command,
			idempotency_key="COD-019-map-upsert",
			correlation_id="COD-019-map-replay",
		)
		self.assertEqual(first["revision"], 1)
		self.assertFalse(first["idempotency_replayed"])
		self.assertTrue(replay["idempotency_replayed"])
		update = upsert_organization_mapping(
			mapping_command(
				version_name,
				"OUTPATIENT",
				expected_revision=1,
				remarks="修订说明",
			),
			idempotency_key="COD-019-map-update",
			correlation_id="COD-019-map-update",
		)
		self.assertEqual(update["revision"], 2)
		self.assertEqual(update["changed_fields"], ["remarks"])
		noop = upsert_organization_mapping(
			mapping_command(
				version_name,
				"OUTPATIENT",
				expected_revision=2,
				remarks="修订说明",
			),
			idempotency_key="COD-019-map-noop",
			correlation_id="COD-019-map-noop",
		)
		self.assertFalse(noop["changed"])
		self.assertEqual(noop["revision"], 2)

	def test_permission_and_draft_state_fail_without_durable_reservation(self) -> None:
		version = create_version(publish=False, suffix="draft")
		version_name = cast(str, version["name"])
		before = frappe.db.count("HRP Service Idempotency")
		with self.assertRaises(IoneApplicationError) as state_error:
			upsert_organization_mapping(
				mapping_command(version_name, "OUTPATIENT"),
				idempotency_key="COD-019-map-draft",
				correlation_id="COD-019-map-draft",
			)
		self.assertEqual(state_error.exception.code, "IONE-CORE-0006")
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)

		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			self.assertRaises(IoneApplicationError) as denied,
		):
			upsert_organization_mapping(
				mapping_command(version_name, "OUTPATIENT"),
				idempotency_key="COD-019-map-denied",
				correlation_id="COD-019-map-denied",
			)
		self.assertEqual(denied.exception.code, "IONE-CORE-0002")
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)

	def test_standard_target_company_enabled_and_uniqueness_are_enforced(self) -> None:
		version = create_version(suffix="target")
		version_name = cast(str, version["name"])
		department_root = frappe.db.get_value(
			"Department",
			{"parent_department": ("is", "not set"), "is_group": 1},
			"name",
		)
		with self.assertRaises(IoneApplicationError) as company_error:
			upsert_organization_mapping(
				build_organization_mapping_upsert(
					organization_version=version_name,
					organization_unit=unit_name(version_name, "OUTPATIENT"),
					department=department_root,
				),
				idempotency_key="COD-019-map-company",
				correlation_id="COD-019-map-company",
			)
		self.assertEqual(company_error.exception.code, "IONE-CORE-0005")

		department, _ = standard_targets()
		frappe.db.set_value("Department", department, "disabled", 1, update_modified=False)
		with self.assertRaises(IoneApplicationError) as disabled_error:
			upsert_organization_mapping(
				mapping_command(version_name, "OUTPATIENT"),
				idempotency_key="COD-019-map-disabled",
				correlation_id="COD-019-map-disabled",
			)
		self.assertEqual(disabled_error.exception.code, "IONE-CORE-0006")
		frappe.db.set_value("Department", department, "disabled", 0, update_modified=False)

		upsert_organization_mapping(
			mapping_command(version_name, "OUTPATIENT"),
			idempotency_key="COD-019-map-unique-parent",
			correlation_id="COD-019-map-unique-parent",
		)
		with self.assertRaises(IoneApplicationError) as duplicate:
			upsert_organization_mapping(
				mapping_command(version_name, "CARDIOLOGY"),
				idempotency_key="COD-019-map-duplicate",
				correlation_id="COD-019-map-duplicate",
			)
		self.assertEqual(duplicate.exception.code, "IONE-CORE-0005")

	def test_standard_trees_must_follow_organization_ancestry(self) -> None:
		version = create_version(suffix="tree")
		version_name = cast(str, version["name"])
		upsert_organization_mapping(
			mapping_command(version_name, "OUTPATIENT", target_kind="parent"),
			idempotency_key="COD-019-map-tree-parent",
			correlation_id="COD-019-map-tree-parent",
		)
		with self.assertRaises(IoneApplicationError) as mismatch:
			upsert_organization_mapping(
				mapping_command(version_name, "CARDIOLOGY", target_kind="unrelated"),
				idempotency_key="COD-019-map-tree-mismatch",
				correlation_id="COD-019-map-tree-mismatch",
			)
		self.assertEqual(mismatch.exception.code, "IONE-CORE-0005")
		child = upsert_organization_mapping(
			mapping_command(version_name, "CARDIOLOGY", target_kind="child"),
			idempotency_key="COD-019-map-tree-child",
			correlation_id="COD-019-map-tree-child",
		)
		self.assertEqual(child["revision"], 1)

	def test_resolve_supports_direct_and_effective_date_with_integration_role(self) -> None:
		version = create_version(suffix="resolve")
		version_name = cast(str, version["name"])
		created = upsert_organization_mapping(
			mapping_command(version_name, "OUTPATIENT"),
			idempotency_key="COD-019-map-resolve",
			correlation_id="COD-019-map-resolve",
		)
		with patch(
			"medops.services.errors.frappe.get_roles",
			return_value=["HRP Integration User"],
		):
			direct = resolve_organization_mapping(
				build_organization_mapping_resolve(
					organization_unit=created["organization_unit"],
				),
				correlation_id="COD-019-resolve-direct",
			)
			dated = resolve_organization_mapping(
				build_organization_mapping_resolve(
					hospital=TEST_HOSPITAL,
					unit_code="OUTPATIENT",
					effective_on="2026-06-30",
				),
				correlation_id="COD-019-resolve-dated",
			)
		self.assertEqual(direct["name"], created["name"])
		self.assertEqual(dated["name"], created["name"])

	def test_disabled_mapping_is_not_operationally_resolvable(self) -> None:
		version = create_version(suffix="disabled")
		version_name = cast(str, version["name"])
		created = upsert_organization_mapping(
			mapping_command(version_name, "OUTPATIENT", enabled=False),
			idempotency_key="COD-019-map-create-disabled",
			correlation_id="COD-019-map-create-disabled",
		)

		with self.assertRaises(IoneApplicationError) as raised:
			resolve_organization_mapping(
				build_organization_mapping_resolve(
					organization_unit=created["organization_unit"],
				),
				correlation_id="COD-019-map-disabled-resolve",
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0006")

	def test_audit_and_idempotency_redact_standard_names_and_remarks(self) -> None:
		version = create_version(suffix="redaction")
		version_name = cast(str, version["name"])
		sentinel = "COD-019敏感映射说明"
		command = mapping_command(
			version_name,
			"OUTPATIENT",
			remarks=sentinel,
		)
		with patch("medops.services.audit_context.frappe.logger") as logger:
			upsert_organization_mapping(
				command,
				idempotency_key="COD-019-map-redaction",
				correlation_id="COD-019-map-redaction",
			)
		audit_payload = json.dumps(
			[
				call.args[0]
				for level in (logger.return_value.info, logger.return_value.warning)
				for call in level.call_args_list
			],
			ensure_ascii=False,
		)
		department, cost_center = standard_targets()
		self.assertNotIn(sentinel, audit_payload)
		self.assertNotIn(department, audit_payload)
		self.assertNotIn(cost_center, audit_payload)
		record = frappe.get_doc(
			"HRP Service Idempotency",
			idempotency_record_name(
				UpsertOrganizationMappingService.definition.name,
				"COD-019-map-redaction",
			),
		)
		self.assertNotIn(sentinel, record.as_json())


class TestOrganizationMappingAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_company_fixture()

	def setUp(self) -> None:
		super().setUp()
		reset_mapping_test_state()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def _create_published_version_over_http(self) -> dict[str, object]:
		hospital = self.post(
			self.method(UPSERT_HOSPITAL_METHOD),
			{
				"code": TEST_HOSPITAL,
				"company": TEST_COMPANY,
				"display_name": "COD-019 HTTP测试医院",
				"expected_revision": 0,
			},
			headers={"Idempotency-Key": "COD-019-http-hospital"},
		)
		self.assertEqual(hospital.status_code, 200, hospital.get_data(as_text=True))
		version_response = self.post(
			self.method(CREATE_VERSION_METHOD),
			{
				"hospital": TEST_HOSPITAL,
				"effective_from": "2026-01-01",
				"version_label": "COD-019 HTTP组织版本",
			},
			headers={"Idempotency-Key": "COD-019-http-version"},
		)
		self.assertEqual(
			version_response.status_code,
			200,
			version_response.get_data(as_text=True),
		)
		version = version_response.get_json()["message"]
		replaced_response = self.post(
			self.method(REPLACE_HIERARCHY_METHOD),
			{
				"organization_version": version["name"],
				"expected_revision": version["revision"],
				"nodes": json.dumps(hierarchy_nodes(), ensure_ascii=False),
			},
			headers={"Idempotency-Key": "COD-019-http-hierarchy"},
		)
		self.assertEqual(
			replaced_response.status_code,
			200,
			replaced_response.get_data(as_text=True),
		)
		replaced = replaced_response.get_json()["message"]
		published_response = self.post(
			self.method(PUBLISH_VERSION_METHOD),
			{
				"organization_version": version["name"],
				"expected_revision": replaced["revision"],
			},
			headers={"Idempotency-Key": "COD-019-http-publish"},
		)
		self.assertEqual(
			published_response.status_code,
			200,
			published_response.get_data(as_text=True),
		)
		return cast(dict[str, object], published_response.get_json()["message"])

	def test_http_upsert_and_effective_date_resolution(self) -> None:
		version = self._create_published_version_over_http()
		department, cost_center = standard_targets()
		mapping_response = self.post(
			self.method(UPSERT_MAPPING_METHOD),
			{
				"organization_version": version["name"],
				"organization_unit": f"{version['name']}-OUTPATIENT",
				"department": department,
				"cost_center": cost_center,
				"expected_revision": 0,
			},
			headers={
				"Idempotency-Key": "COD-019-http-mapping",
				"X-Correlation-ID": "COD-019-http-mapping",
			},
		)
		self.assertEqual(
			mapping_response.status_code,
			200,
			mapping_response.get_data(as_text=True),
		)
		resolved_response = self.get(
			self.method(RESOLVE_MAPPING_METHOD),
			{
				"hospital": TEST_HOSPITAL,
				"unit_code": "OUTPATIENT",
				"effective_on": "2026-06-30",
			},
		)
		self.assertEqual(
			resolved_response.status_code,
			200,
			resolved_response.get_data(as_text=True),
		)
		resolved = resolved_response.get_json()["message"]
		self.assertEqual(resolved["department"], department)
		self.assertEqual(resolved["cost_center"], cost_center)
		self.assertTrue(resolved_response.headers["X-Correlation-ID"])

	def test_http_write_requires_idempotency_header(self) -> None:
		version = self._create_published_version_over_http()
		department, cost_center = standard_targets()
		response = self.post(
			self.method(UPSERT_MAPPING_METHOD),
			{
				"organization_version": version["name"],
				"organization_unit": f"{version['name']}-OUTPATIENT",
				"department": department,
				"cost_center": cost_center,
				"expected_revision": 0,
			},
			headers={"X-Correlation-ID": "COD-019-http-missing-key"},
		)
		self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
		self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0003")
