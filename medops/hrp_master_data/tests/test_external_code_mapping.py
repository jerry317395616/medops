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
from medops.common.external_code_mapping import (
	build_external_code_mapping_resolve,
	build_external_code_mapping_upsert,
	build_internal_code_mapping_resolve,
)
from medops.common.master_data import build_master_data_domain_upsert
from medops.common.organization import build_hospital_upsert
from medops.hrp_master_data.doctype.hrp_external_code_mapping.hrp_external_code_mapping import (
	HRPExternalCodeMapping,
)
from medops.hrp_master_data.permissions import (
	can_read_external_code_mapping,
	external_code_mapping_query,
)
from medops.hrp_master_data.services.external_code_mapping import (
	ResolveExternalCodeMappingService,
	ResolveInternalCodeMappingService,
	UpsertExternalCodeMappingService,
	resolve_external_code_mapping,
	resolve_internal_code_mapping,
	upsert_external_code_mapping,
)
from medops.hrp_master_data.services.master_data import (
	UpsertMasterDataDomainService,
	upsert_master_data_domain,
)
from medops.hrp_organization.services.organization import (
	UpsertHospitalService,
	upsert_hospital,
)
from medops.setup.master_data import ensure_master_data_governance

UPSERT_MAPPING_METHOD = "medops.api.v1.master_data.upsert_external_code_mapping"
RESOLVE_EXTERNAL_METHOD = "medops.api.v1.master_data.resolve_external_code_mapping"
RESOLVE_INTERNAL_METHOD = "medops.api.v1.master_data.resolve_internal_code_mapping"

TEST_DOMAIN = "COD020-ITEM"
TEST_COMPANY = "COD-021测试医疗法人"
TEST_COMPANY_ABBR = "C021"
TEST_OTHER_COMPANY = "COD-021其他法人"
TEST_OTHER_COMPANY_ABBR = "C21B"
TEST_HOSPITAL = "COD021-HOSPITAL"
TEST_ITEM = "COD021-ITEM-001"
TEST_OTHER_ITEM = "COD021-ITEM-002"
TEST_ITEM_GROUP = "COD021 Items"
TEST_ITEM_GROUP_ROOT = "COD021 All Item Groups"
TEST_STOCK_UOM = "COD021-UOM"
TEST_INTEGRATION_USER = "cod021-integration@example.com"
TEST_PLAIN_USER = "cod021-plain@example.com"
MAPPING_SERVICE_NAMES = (
	UpsertExternalCodeMappingService.definition.name,
	ResolveExternalCodeMappingService.definition.name,
	ResolveInternalCodeMappingService.definition.name,
	UpsertMasterDataDomainService.definition.name,
	UpsertHospitalService.definition.name,
)


def ensure_external_code_mapping_fixtures() -> None:
	for company, abbr in (
		(TEST_COMPANY, TEST_COMPANY_ABBR),
		(TEST_OTHER_COMPANY, TEST_OTHER_COMPANY_ABBR),
	):
		if not frappe.db.exists("Company", company):
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": company,
					"abbr": abbr,
					"country": "China",
					"default_currency": "CNY",
				}
			).insert(ignore_permissions=True)
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
	for item_code in (TEST_ITEM, TEST_OTHER_ITEM):
		if not frappe.db.exists("Item", item_code):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": item_code,
					"item_group": TEST_ITEM_GROUP,
					"stock_uom": TEST_STOCK_UOM,
					"is_stock_item": 0,
					"disabled": 0,
				}
			).insert(ignore_permissions=True)
	for email, role in (
		(TEST_INTEGRATION_USER, "HRP Integration User"),
		(TEST_PLAIN_USER, None),
	):
		if frappe.db.exists("User", email):
			user = frappe.get_doc("User", email)
		else:
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "COD-021",
					"last_name": "Integration" if role else "Plain",
					"enabled": 1,
					"send_welcome_email": 0,
				}
			)
			user.insert(ignore_permissions=True)
		if role and role not in frappe.get_roles(email):
			user.add_roles(role)
	frappe.local.db.commit()


def reset_external_code_mapping_state() -> None:
	frappe.set_user("Administrator")
	frappe.db.delete("HRP External Code Mapping", {"master_data_domain": TEST_DOMAIN})
	frappe.db.delete("HRP Hospital", {"name": TEST_HOSPITAL})
	frappe.db.delete(
		"HRP Service Idempotency",
		{"service_name": ("in", MAPPING_SERVICE_NAMES)},
	)
	frappe.db.set_value("Item", TEST_ITEM, "disabled", 0, update_modified=False)
	frappe.db.set_value("Item", TEST_OTHER_ITEM, "disabled", 0, update_modified=False)


def create_mapping_context(*, suffix: str) -> None:
	upsert_hospital(
		build_hospital_upsert(
			code=TEST_HOSPITAL,
			company=TEST_COMPANY,
			display_name="COD-021测试医院",
			expected_revision=0,
		),
		idempotency_key=f"COD-021-hospital-{suffix}",
	)
	if not frappe.db.exists("HRP Master Data Domain", TEST_DOMAIN):
		upsert_master_data_domain(
			build_master_data_domain_upsert(
				code=TEST_DOMAIN,
				display_name="COD-020物料主数据",
				target_doctype="Item",
				expected_revision=0,
			),
			idempotency_key=f"COD-021-domain-{suffix}",
		)
	else:
		target_doctype, enabled = frappe.db.get_value(
			"HRP Master Data Domain",
			TEST_DOMAIN,
			["target_doctype", "enabled"],
		)
		if target_doctype != "Item" or not bool(enabled):
			raise AssertionError("COD-020 Item master-data domain must be operational")


def mapping_command(
	*,
	external_code: str = "001A",
	internal_name: str = TEST_ITEM,
	mapping_name: str | None = None,
	expected_revision: int = 0,
	external_label: str = "COD-021外部物料",
	enabled: bool = True,
	valid_from: str = "2026-08-01",
	valid_to: str | None = None,
	company: str = TEST_COMPANY,
	remarks: str | None = "COD-021受控映射",
):
	return build_external_code_mapping_upsert(
		mapping_name=mapping_name,
		master_data_domain=TEST_DOMAIN,
		company=company,
		hospital=TEST_HOSPITAL,
		external_system="HIS",
		external_code=external_code,
		external_label=external_label,
		internal_name=internal_name,
		enabled=enabled,
		valid_from=valid_from,
		valid_to=valid_to,
		expected_revision=expected_revision,
		remarks=remarks,
	)


class TestExternalCodeMapping(IntegrationTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_external_code_mapping_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_external_code_mapping_state()

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	def test_metadata_migration_workspace_and_service_only_write(self) -> None:
		create_mapping_context(suffix="metadata")
		meta = frappe.get_meta("HRP External Code Mapping")
		self.assertEqual(meta.module, "HRP Master Data")
		self.assertEqual(meta.get_field("external_code").label, "外部编码")
		self.assertEqual(meta.get_field("internal_name").options, "target_doctype")
		self.assertEqual(len(meta.fields), 17)
		self.assertFalse(any(permission.write for permission in meta.permissions))
		self.assertSetEqual(
			{permission.role for permission in meta.permissions},
			{
				"System Manager",
				"HRP System Manager",
				"HRP Data Steward",
				"HRP Integration User",
			},
		)
		self.assertEqual(ensure_master_data_governance()["schema_version"], 1)
		self.assertEqual(ensure_master_data_governance()["schema_version"], 1)
		index_rows = frappe.db.sql(
			"SHOW INDEX FROM `tabHRP External Code Mapping`",
			as_dict=True,
		)
		index_names = {str(row.Key_name) for row in index_rows}
		self.assertTrue(
			{
				"uniq_hrp_external_code_source",
				"uniq_hrp_external_code_target",
				"idx_hrp_external_code_effectivity",
			}.issubset(index_names)
		)
		workspace = frappe.get_doc("Workspace", "HRP Master Data")
		self.assertIn(
			"HRP External Code Mapping",
			{shortcut.link_to for shortcut in workspace.shortcuts},
		)
		sidebars = get_sidebar_items()
		default_workspace_map = build_default_workspace_map(sidebars)
		self.assertEqual(default_workspace_map["HRP External Code Mapping"], "HRP")

		direct = frappe.get_doc(
			{
				"doctype": "HRP External Code Mapping",
				"master_data_domain": TEST_DOMAIN,
				"target_doctype": "Item",
				"internal_name": TEST_ITEM,
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"external_system": "HIS",
				"external_code": "DIRECT",
				"valid_from": "2026-08-01",
				"revision": 1,
			}
		)
		with self.assertRaises(IoneApplicationError) as denied:
			direct.insert(ignore_permissions=True)
		self.assertEqual(denied.exception.code, "IONE-CORE-0008")

	def test_upsert_is_idempotent_revisioned_and_identity_is_immutable(self) -> None:
		create_mapping_context(suffix="revision")
		command = mapping_command()
		created = upsert_external_code_mapping(
			command,
			idempotency_key="COD-021-mapping-create",
		)
		replay = upsert_external_code_mapping(
			command,
			idempotency_key="COD-021-mapping-create",
		)
		self.assertEqual(created["revision"], 1)
		self.assertTrue(replay["idempotency_replayed"])
		updated = upsert_external_code_mapping(
			mapping_command(
				mapping_name=cast(str, created["name"]),
				expected_revision=1,
				external_label="COD-021修订名称",
			),
			idempotency_key="COD-021-mapping-update",
		)
		self.assertEqual(updated["revision"], 2)
		self.assertEqual(updated["changed_fields"], ["external_label"])
		with self.assertRaises(IoneApplicationError) as stale:
			upsert_external_code_mapping(
				mapping_command(
					mapping_name=cast(str, created["name"]),
					expected_revision=1,
					external_label="stale",
				),
				idempotency_key="COD-021-mapping-stale",
			)
		self.assertEqual(stale.exception.code, "IONE-CORE-0005")
		with self.assertRaises(IoneApplicationError) as immutable:
			upsert_external_code_mapping(
				mapping_command(
					mapping_name=cast(str, created["name"]),
					expected_revision=2,
					external_code="DIFFERENT",
				),
				idempotency_key="COD-021-mapping-identity",
			)
		self.assertEqual(immutable.exception.code, "IONE-CORE-0008")

	def test_uniqueness_and_bidirectional_effectivity(self) -> None:
		create_mapping_context(suffix="resolve")
		created = upsert_external_code_mapping(
			mapping_command(valid_to="2026-08-31"),
			idempotency_key="COD-021-resolve-create",
		)
		inbound = resolve_external_code_mapping(
			build_external_code_mapping_resolve(
				master_data_domain=TEST_DOMAIN,
				company=TEST_COMPANY,
				hospital=TEST_HOSPITAL,
				external_system="HIS",
				external_code="001A",
				effective_on="2026-08-15",
			)
		)
		outbound = resolve_internal_code_mapping(
			build_internal_code_mapping_resolve(
				master_data_domain=TEST_DOMAIN,
				company=TEST_COMPANY,
				hospital=TEST_HOSPITAL,
				external_system="HIS",
				internal_name=TEST_ITEM,
				effective_on="2026-08-15",
			)
		)
		self.assertEqual(inbound["internal_name"], TEST_ITEM)
		self.assertEqual(outbound["external_code"], "001A")
		for effective_on in ("2026-07-31", "2026-09-01"):
			with self.subTest(effective_on=effective_on), self.assertRaises(IoneApplicationError) as missing:
				resolve_external_code_mapping(
					build_external_code_mapping_resolve(
						master_data_domain=TEST_DOMAIN,
						company=TEST_COMPANY,
						hospital=TEST_HOSPITAL,
						external_system="HIS",
						external_code="001A",
						effective_on=effective_on,
					)
				)
			self.assertEqual(missing.exception.code, "IONE-CORE-0004")
		for suffix, command in (
			("source", mapping_command(internal_name=TEST_OTHER_ITEM)),
			("target", mapping_command(external_code="002B")),
		):
			with self.subTest(suffix=suffix), self.assertRaises(IoneApplicationError) as conflict:
				upsert_external_code_mapping(
					command,
					idempotency_key=f"COD-021-conflict-{suffix}",
				)
			self.assertEqual(conflict.exception.code, "IONE-CORE-0005")
		self.assertEqual(created["name"], inbound["name"])

	def test_database_uniqueness_races_are_reported_as_conflicts(self) -> None:
		create_mapping_context(suffix="race")
		with (
			patch.object(
				HRPExternalCodeMapping,
				"insert",
				side_effect=frappe.DuplicateEntryError("concurrent source identity"),
			),
			self.assertRaises(IoneApplicationError) as create_conflict,
		):
			upsert_external_code_mapping(
				mapping_command(),
				idempotency_key="COD-021-race-create",
			)
		self.assertEqual(create_conflict.exception.code, "IONE-CORE-0005")

		created = upsert_external_code_mapping(
			mapping_command(),
			idempotency_key="COD-021-race-baseline",
		)
		with (
			patch.object(
				HRPExternalCodeMapping,
				"save",
				side_effect=frappe.DuplicateEntryError("concurrent target identity"),
			),
			self.assertRaises(IoneApplicationError) as update_conflict,
		):
			upsert_external_code_mapping(
				mapping_command(
					mapping_name=cast(str, created["name"]),
					expected_revision=1,
					internal_name=TEST_OTHER_ITEM,
				),
				idempotency_key="COD-021-race-update",
			)
		self.assertEqual(update_conflict.exception.code, "IONE-CORE-0005")

	def test_scope_target_and_role_checks_precede_idempotency(self) -> None:
		create_mapping_context(suffix="guard")
		before = frappe.db.count("HRP Service Idempotency")
		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			self.assertRaises(IoneApplicationError) as role_denied,
		):
			upsert_external_code_mapping(
				mapping_command(),
				idempotency_key="COD-021-role-denied",
			)
		self.assertEqual(role_denied.exception.code, "IONE-CORE-0002")
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)
		with self.assertRaises(IoneApplicationError) as wrong_company:
			upsert_external_code_mapping(
				mapping_command(company=TEST_OTHER_COMPANY),
				idempotency_key="test-key-01",
			)
		self.assertEqual(wrong_company.exception.code, "IONE-CORE-0005")
		frappe.db.set_value("Item", TEST_ITEM, "disabled", 1, update_modified=False)
		with self.assertRaises(IoneApplicationError) as disabled:
			upsert_external_code_mapping(
				mapping_command(),
				idempotency_key="COD-021-disabled-target",
			)
		self.assertEqual(disabled.exception.code, "IONE-CORE-0006")

	def test_permissions_and_audit_are_redacted(self) -> None:
		create_mapping_context(suffix="audit")
		sentinel = "COD-021-sensitive-remarks"
		with patch("medops.services.audit_context.frappe.logger") as logger:
			created = upsert_external_code_mapping(
				mapping_command(remarks=sentinel, external_label="Sensitive external label"),
				idempotency_key="COD-021-audit-create",
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
		self.assertNotIn("Sensitive external label", payload)
		record = frappe.get_doc(
			"HRP Service Idempotency",
			idempotency_record_name(
				UpsertExternalCodeMappingService.definition.name,
				"COD-021-audit-create",
			),
		)
		self.assertNotIn(sentinel, record.request_fingerprint)

		frappe.set_user(TEST_INTEGRATION_USER)
		self.assertEqual(external_code_mapping_query(), "")
		mapping = frappe.get_doc("HRP External Code Mapping", created["name"])
		self.assertTrue(can_read_external_code_mapping(mapping, ptype="read"))
		self.assertIn(
			created["name"],
			frappe.get_list("HRP External Code Mapping", pluck="name"),
		)
		frappe.set_user(TEST_PLAIN_USER)
		self.assertEqual(external_code_mapping_query(), "1=0")
		self.assertFalse(can_read_external_code_mapping(mapping, ptype="read"))
		frappe.set_user("Guest")
		self.assertEqual(external_code_mapping_query(), "1=0")
		self.assertFalse(can_read_external_code_mapping(mapping, ptype="read"))


class TestExternalCodeMappingAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_external_code_mapping_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_external_code_mapping_state()
		create_mapping_context(suffix="http")
		frappe.local.db.commit()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def _payload(self) -> dict[str, object]:
		return {
			"master_data_domain": TEST_DOMAIN,
			"company": TEST_COMPANY,
			"hospital": TEST_HOSPITAL,
			"external_system": "HIS",
			"external_code": "HTTP-001",
			"external_label": "HTTP external item",
			"internal_name": TEST_ITEM,
			"valid_from": "2026-08-01",
			"expected_revision": 0,
		}

	def test_http_write_requires_idempotency_header(self) -> None:
		response = self.post(
			self.method(UPSERT_MAPPING_METHOD),
			self._payload(),
			headers={"X-Correlation-ID": "COD-021-http-missing-key"},
		)
		self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
		self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0003")

	def test_http_upsert_and_bidirectional_queries(self) -> None:
		created_response = self.post(
			self.method(UPSERT_MAPPING_METHOD),
			self._payload(),
			headers={
				"Idempotency-Key": "COD-021-http-mapping",
				"X-Correlation-ID": "COD-021-http-mapping",
			},
		)
		self.assertEqual(
			created_response.status_code,
			200,
			created_response.get_data(as_text=True),
		)
		created = created_response.get_json()["message"]
		inbound_response = self.get(
			self.method(RESOLVE_EXTERNAL_METHOD),
			{
				"master_data_domain": TEST_DOMAIN,
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"external_system": "HIS",
				"external_code": "HTTP-001",
				"effective_on": "2026-08-03",
			},
			headers={"X-Correlation-ID": "COD-021-http-inbound"},
		)
		outbound_response = self.get(
			self.method(RESOLVE_INTERNAL_METHOD),
			{
				"master_data_domain": TEST_DOMAIN,
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"external_system": "HIS",
				"internal_name": TEST_ITEM,
				"effective_on": "2026-08-03",
			},
			headers={"X-Correlation-ID": "COD-021-http-outbound"},
		)
		self.assertEqual(
			inbound_response.status_code,
			200,
			inbound_response.get_data(as_text=True),
		)
		self.assertEqual(
			outbound_response.status_code,
			200,
			outbound_response.get_data(as_text=True),
		)
		self.assertEqual(inbound_response.get_json()["message"]["name"], created["name"])
		self.assertEqual(outbound_response.get_json()["message"]["external_code"], "HTTP-001")
