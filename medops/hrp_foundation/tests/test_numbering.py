from __future__ import annotations

from typing import cast
from unittest.mock import patch

import frappe
from frappe.boot import build_default_workspace_map, get_sidebar_items
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.domain_service import idempotency_record_name
from medops.common.error_catalog import IoneApplicationError
from medops.common.numbering import (
	build_number_allocation,
	build_numbering_scheme_upsert,
	counter_key_for,
)
from medops.common.organization import build_hospital_upsert
from medops.hrp_foundation.doctype.hrp_number_reservation.hrp_number_reservation import (
	HRPNumberReservation,
)
from medops.hrp_foundation.doctype.hrp_numbering_scheme.hrp_numbering_scheme import (
	HRPNumberingScheme,
)
from medops.hrp_foundation.permissions import (
	can_read_number_reservation,
	number_reservation_query,
)
from medops.hrp_foundation.services.numbering import (
	AllocateNumberService,
	UpsertNumberingSchemeService,
	allocate_number,
	get_number_reservation,
	upsert_numbering_scheme,
)
from medops.hrp_organization.services.organization import (
	UpsertHospitalService,
	upsert_hospital,
)
from medops.setup.numbering import ensure_numbering_governance

UPSERT_SCHEME_METHOD = "medops.api.v1.core.numbering.upsert_scheme"
NEXT_NUMBER_METHOD = "medops.api.v1.core.numbering.next"
GET_RESERVATION_METHOD = "medops.api.v1.core.numbering.get_reservation"

TEST_COMPANY = "COD-023测试医疗法人"
TEST_COMPANY_ABBR = "C023"
TEST_HOSPITAL = "COD023-HOSPITAL"
TEST_WAREHOUSE_TYPE = "Transit"
TEST_HRP_USER = "cod023-user@example.com"
TEST_AUDITOR = "cod023-auditor@example.com"
TEST_PLAIN_USER = "cod023-plain@example.com"
NUMBERING_SERVICE_NAMES = (
	AllocateNumberService.definition.name,
	UpsertNumberingSchemeService.definition.name,
	UpsertHospitalService.definition.name,
)


def request_key(*parts: str) -> str:
	return "-".join(("COD", "023", *parts))


def ensure_numbering_fixtures() -> None:
	frappe.set_user("Administrator")
	if not frappe.db.exists("Warehouse Type", TEST_WAREHOUSE_TYPE):
		frappe.get_doc(
			{
				"doctype": "Warehouse Type",
				"name": TEST_WAREHOUSE_TYPE,
				"description": "COD-023测试中转仓类型",
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
	if not frappe.db.exists("HRP Hospital", TEST_HOSPITAL):
		upsert_hospital(
			build_hospital_upsert(
				code=TEST_HOSPITAL,
				company=TEST_COMPANY,
				display_name="COD-023测试医院",
				expected_revision=0,
			),
			idempotency_key=request_key("hospital", "fixture"),
		)
	else:
		hospital = frappe.get_doc("HRP Hospital", TEST_HOSPITAL)
		if hospital.company != TEST_COMPANY:
			raise AssertionError("COD-023 hospital fixture belongs to an unexpected company")
		fixture_date = "2026-08-08"
		needs_repair = (
			not bool(hospital.enabled)
			or (hospital.valid_from and str(hospital.valid_from) > fixture_date)
			or (hospital.valid_to and str(hospital.valid_to) < fixture_date)
		)
		if needs_repair:
			upsert_hospital(
				build_hospital_upsert(
					code=TEST_HOSPITAL,
					company=TEST_COMPANY,
					display_name=hospital.display_name,
					enabled=True,
					expected_revision=int(hospital.revision),
				),
				idempotency_key=request_key("hospital", "repair", str(hospital.revision)),
			)
	for email, role in (
		(TEST_HRP_USER, "HRP User"),
		(TEST_AUDITOR, "HRP Auditor"),
		(TEST_PLAIN_USER, None),
	):
		if frappe.db.exists("User", email):
			user = frappe.get_doc("User", email)
		else:
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "COD-023",
					"last_name": role or "Plain",
					"enabled": 1,
					"send_welcome_email": 0,
				}
			)
			user.insert(ignore_permissions=True)
		if role and role not in frappe.get_roles(email):
			user.add_roles(role)
	frappe.local.db.commit()


def reset_numbering_state() -> None:
	frappe.set_user("Administrator")
	counter_keys = frappe.get_all(
		"HRP Number Reservation",
		filters={"scheme_code": ("like", "COD023-%")},
		pluck="counter_key",
	)
	frappe.db.delete("HRP Number Reservation", {"scheme_code": ("like", "COD023-%")})
	frappe.db.delete("HRP Numbering Scheme", {"code": ("like", "COD023-%")})
	frappe.db.delete(
		"HRP Service Idempotency",
		{"service_name": ("in", NUMBERING_SERVICE_NAMES)},
	)
	for key in set(counter_keys):
		frappe.db.sql("DELETE FROM `tabSeries` WHERE name = %s", key)


def scheme_command(
	*,
	code: str = "COD023-PO",
	display_name: str = "采购订单编号",
	template: str = "PO-{YYYY}{MM}-{DIM:department}-{SEQ}",
	reset_policy: str = "Monthly",
	sequence_digits: int = 4,
	start_number: int = 1,
	enabled: bool = True,
	valid_from: str | None = "2026-01-01",
	valid_to: str | None = None,
	expected_revision: int = 0,
	scheme_name: str | None = None,
):
	return build_numbering_scheme_upsert(
		scheme_name=scheme_name,
		code=code,
		display_name=display_name,
		company=TEST_COMPANY,
		hospital=TEST_HOSPITAL,
		template=template,
		reset_policy=reset_policy,
		sequence_digits=sequence_digits,
		start_number=start_number,
		enabled=enabled,
		valid_from=valid_from,
		valid_to=valid_to,
		expected_revision=expected_revision,
		remarks="COD-023受控编号方案",
	)


def allocation_command(
	*,
	scheme: str = "COD023-PO",
	dimensions: object = None,
	business_date: str = "2026-08-08",
):
	return build_number_allocation(
		scheme=scheme,
		dimensions={"department": "FIN"} if dimensions is None else dimensions,
		business_date=business_date,
	)


class TestNumbering(IntegrationTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_numbering_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_numbering_state()

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	def _create_scheme(self, *, code: str = "COD023-PO", template: str | None = None):
		return upsert_numbering_scheme(
			scheme_command(code=code, template=template or "PO-{YYYY}{MM}-{DIM:department}-{SEQ}"),
			idempotency_key=f"COD-023-scheme-{code}",
		)

	def test_metadata_migration_workspace_and_service_only_write(self) -> None:
		scheme_meta = frappe.get_meta("HRP Numbering Scheme")
		reservation_meta = frappe.get_meta("HRP Number Reservation")
		self.assertEqual(scheme_meta.module, "HRP Foundation")
		self.assertEqual(scheme_meta.get_field("template").label, "编号模板")
		self.assertEqual(reservation_meta.get_field("number").label, "业务编号")
		self.assertFalse(any(permission.write for permission in scheme_meta.permissions))
		self.assertFalse(any(permission.write for permission in reservation_meta.permissions))
		self.assertEqual(ensure_numbering_governance()["schema_version"], 1)
		self.assertEqual(ensure_numbering_governance()["schema_version"], 1)
		for doctype, expected in (
			("HRP Numbering Scheme", {"idx_hrp_numbering_scheme_effectivity"}),
			(
				"HRP Number Reservation",
				{
					"uniq_hrp_number_reservation_number",
					"uniq_hrp_number_reservation_token",
					"idx_hrp_number_reservation_scheme_date",
					"idx_hrp_number_reservation_owner",
				},
			),
		):
			indexes = {
				str(row.Key_name) for row in frappe.db.sql(f"SHOW INDEX FROM `tab{doctype}`", as_dict=True)
			}
			self.assertTrue(expected.issubset(indexes))

		workspace = frappe.get_doc("Workspace", "HRP")
		shortcuts = {shortcut.link_to for shortcut in workspace.shortcuts}
		self.assertTrue({"HRP Numbering Scheme", "HRP Number Reservation"}.issubset(shortcuts))
		workspace_map = build_default_workspace_map(get_sidebar_items())
		self.assertEqual(workspace_map["HRP Numbering Scheme"], "HRP")
		self.assertEqual(workspace_map["HRP Number Reservation"], "HRP")

		direct = frappe.get_doc(
			{
				"doctype": "HRP Numbering Scheme",
				"code": "COD023-DIRECT",
				"display_name": "直接写入",
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"template": "DIRECT-{SEQ}",
				"reset_policy": "Never",
				"sequence_digits": 4,
				"start_number": 1,
				"enabled": 1,
				"revision": 1,
			}
		)
		with self.assertRaises(IoneApplicationError) as denied:
			direct.insert(ignore_permissions=True)
		self.assertEqual(denied.exception.code, "IONE-CORE-0008")

	def test_scheme_is_idempotent_revisioned_and_format_locks_after_allocation(self) -> None:
		command = scheme_command()
		created = upsert_numbering_scheme(command, idempotency_key=request_key("scheme", "create"))
		replay = upsert_numbering_scheme(command, idempotency_key=request_key("scheme", "create"))
		self.assertEqual(created["revision"], 1)
		self.assertTrue(replay["idempotency_replayed"])

		updated = upsert_numbering_scheme(
			scheme_command(
				scheme_name=cast(str, created["name"]),
				expected_revision=1,
				display_name="采购订单统一编号",
			),
			idempotency_key=request_key("scheme", "update"),
		)
		self.assertEqual(updated["revision"], 2)
		self.assertEqual(updated["changed_fields"], ["display_name"])
		with self.assertRaises(IoneApplicationError) as stale:
			upsert_numbering_scheme(
				scheme_command(scheme_name="COD023-PO", expected_revision=1),
				idempotency_key=request_key("scheme", "stale"),
			)
		self.assertEqual(stale.exception.code, "IONE-CORE-0005")

		allocate_number(allocation_command(), idempotency_key=request_key("allocation", "lock", "format"))
		with self.assertRaises(IoneApplicationError) as immutable:
			upsert_numbering_scheme(
				scheme_command(
					scheme_name="COD023-PO",
					expected_revision=2,
					display_name="采购订单统一编号",
					template="NEW-{YYYY}{MM}-{DIM:department}-{SEQ}",
				),
				idempotency_key=request_key("scheme", "format", "change"),
			)
		self.assertEqual(immutable.exception.code, "IONE-CORE-0008")

	def test_concurrent_scheme_identity_conflict_is_a_controlled_conflict(self) -> None:
		with patch.object(
			HRPNumberingScheme,
			"insert",
			side_effect=frappe.DuplicateEntryError("concurrent scheme code"),
		):
			with self.assertRaises(IoneApplicationError) as conflict:
				upsert_numbering_scheme(
					scheme_command(code="COD023-CONCURRENT"),
					idempotency_key=request_key("concurrent", "scheme"),
				)
		self.assertEqual(conflict.exception.code, "IONE-CORE-0005")

	def test_allocation_is_atomic_idempotent_and_globally_unique(self) -> None:
		self._create_scheme()
		command = allocation_command()
		first = allocate_number(command, idempotency_key=request_key("number", "0001"))
		replay = allocate_number(command, idempotency_key=request_key("number", "0001"))
		second = allocate_number(command, idempotency_key=request_key("number", "0002"))
		self.assertEqual(first["number"], "PO-202608-FIN-0001")
		self.assertEqual(replay["number"], first["number"])
		self.assertEqual(replay["reservation_token"], first["reservation_token"])
		self.assertTrue(replay["idempotency_replayed"])
		self.assertEqual(second["number"], "PO-202608-FIN-0002")
		self.assertEqual(frappe.db.count("HRP Number Reservation"), 2)
		with self.assertRaises(IoneApplicationError) as conflict:
			allocate_number(
				allocation_command(business_date="2026-08-09"),
				idempotency_key=request_key("number", "0001"),
			)
		self.assertEqual(conflict.exception.code, "IONE-CORE-0007")

	def test_dimension_and_reset_buckets_have_independent_sequences(self) -> None:
		self._create_scheme()
		fin = allocate_number(
			allocation_command(dimensions={"department": "FIN"}),
			idempotency_key=request_key("dimension", "fin"),
		)
		hr = allocate_number(
			allocation_command(dimensions={"department": "HR"}),
			idempotency_key=request_key("dimension", "hr"),
		)
		next_month = allocate_number(
			allocation_command(dimensions={"department": "FIN"}, business_date="2026-09-01"),
			idempotency_key=request_key("dimension", "next", "month"),
		)
		self.assertEqual(fin["number"], "PO-202608-FIN-0001")
		self.assertEqual(hr["number"], "PO-202608-HR-0001")
		self.assertEqual(next_month["number"], "PO-202609-FIN-0001")

	def test_invalid_dimension_effectivity_and_disabled_scheme_are_rejected(self) -> None:
		self._create_scheme()
		with self.assertRaises(IoneApplicationError) as missing_dimension:
			allocate_number(
				allocation_command(dimensions={}),
				idempotency_key=request_key("invalid", "dimension"),
			)
		self.assertEqual(missing_dimension.exception.code, "IONE-CORE-0003")
		with self.assertRaises(IoneApplicationError) as before_effective:
			allocate_number(
				allocation_command(business_date="2025-12-31"),
				idempotency_key=request_key("before", "effective"),
			)
		self.assertEqual(before_effective.exception.code, "IONE-CORE-0006")

		upsert_numbering_scheme(
			scheme_command(scheme_name="COD023-PO", expected_revision=1, enabled=False),
			idempotency_key=request_key("disable", "scheme"),
		)
		with self.assertRaises(IoneApplicationError) as disabled:
			allocate_number(
				allocation_command(),
				idempotency_key=request_key("disabled", "allocation"),
			)
		self.assertEqual(disabled.exception.code, "IONE-CORE-0006")

	def test_cross_scheme_number_collision_rolls_back_counter_and_idempotency(self) -> None:
		self._create_scheme(code="COD023-A", template="SAME-{DIM:department}-{SEQ}")
		self._create_scheme(code="COD023-B", template="SAME-{DIM:department}-{SEQ}")
		allocate_number(
			allocation_command(scheme="COD023-A"),
			idempotency_key=request_key("collision", "a"),
		)
		key = request_key("collision", "b")
		with self.assertRaises(IoneApplicationError) as collision:
			allocate_number(
				allocation_command(scheme="COD023-B"),
				idempotency_key=key,
			)
		self.assertEqual(collision.exception.code, "IONE-CORE-0005")
		self.assertFalse(
			frappe.db.exists(
				"HRP Service Idempotency",
				idempotency_record_name(AllocateNumberService.definition.name, key),
			)
		)
		counter_key = counter_key_for(
			scheme="COD023-B",
			reset_policy="Monthly",
			business_date="2026-08-08",
			dimensions={"department": "FIN"},
		)
		self.assertFalse(frappe.db.sql("SELECT name FROM `tabSeries` WHERE name = %s", counter_key))

	def test_reservation_is_immutable_and_permissions_limit_user_rows(self) -> None:
		self._create_scheme()
		frappe.set_user(TEST_HRP_USER)
		allocated = allocate_number(
			allocation_command(),
			idempotency_key=request_key("user", "allocation"),
		)
		reservation = cast(
			HRPNumberReservation,
			frappe.get_doc("HRP Number Reservation", allocated["reservation_token"]),
		)
		self.assertTrue(can_read_number_reservation(reservation, TEST_HRP_USER, "read"))
		self.assertIn(TEST_HRP_USER, number_reservation_query(TEST_HRP_USER))
		self.assertEqual(get_number_reservation(reservation.name)["number"], allocated["number"])
		reservation.number = "MUTATED"
		with self.assertRaises(IoneApplicationError) as immutable:
			reservation.save(ignore_permissions=True)
		self.assertEqual(immutable.exception.code, "IONE-CORE-0008")
		with self.assertRaises(IoneApplicationError):
			reservation.delete(ignore_permissions=True)

		frappe.set_user(TEST_AUDITOR)
		self.assertEqual(number_reservation_query(TEST_AUDITOR), "")
		self.assertTrue(can_read_number_reservation(reservation, TEST_AUDITOR, "read"))
		frappe.set_user(TEST_PLAIN_USER)
		with self.assertRaises(IoneApplicationError) as denied:
			allocate_number(
				allocation_command(),
				idempotency_key=request_key("plain", "user"),
			)
		self.assertEqual(denied.exception.code, "IONE-CORE-0002")
		frappe.set_user("Guest")
		with self.assertRaises(IoneApplicationError) as guest:
			allocate_number(
				allocation_command(),
				idempotency_key=request_key("guest"),
			)
		self.assertEqual(guest.exception.code, "IONE-CORE-0001")

	def test_reservation_rejects_invalid_digest_as_controlled_request(self) -> None:
		self._create_scheme()
		scheme = frappe.get_doc("HRP Numbering Scheme", "COD023-PO")
		test_token = "TEST-" + ("0" * 27)
		reservation = frappe.get_doc(
			{
				"doctype": "HRP Number Reservation",
				"numbering_scheme": scheme.name,
				"scheme_code": scheme.code,
				"number": "COD023-INVALID-DIGEST-0001",
				"reservation_token": test_token,
				"business_date": "2026-08-08",
				"company": TEST_COMPANY,
				"hospital": TEST_HOSPITAL,
				"dimensions_json": '{"department":"FIN"}',
				"dimensions_digest": "not-a-sha256",
				"reset_bucket": "202608",
				"counter_key": "-".join(("IONE", "HRP", "NUM", "COD023", "INVALID", "DIGEST")),
				"sequence_value": 1,
				"scheme_revision": scheme.revision,
				"template_digest": scheme.template_digest,
				"allocated_by": "Administrator",
				"correlation_id": "COD-023-invalid-digest",
				"request_id": "COD-023-invalid-digest-request",
			}
		)
		reservation.flags.numbering_service_write = True
		with self.assertRaises(IoneApplicationError) as invalid:
			reservation.insert(ignore_permissions=True)
		self.assertEqual(invalid.exception.code, "IONE-CORE-0003")

	def test_audit_contains_digests_without_number_or_dimensions(self) -> None:
		self._create_scheme()
		with patch("medops.hrp_foundation.services.numbering.emit_audit_event") as audit:
			allocated = allocate_number(
				allocation_command(),
				idempotency_key=request_key("audit", "allocation"),
			)
		audit_text = str(audit.call_args_list)
		self.assertIn("dimensions_digest", audit_text)
		self.assertIn("template_digest", audit_text)
		self.assertNotIn(cast(str, allocated["number"]), audit_text)
		self.assertNotIn("FIN", audit_text)


class TestNumberingAPI(FrappeAPITestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_numbering_fixtures()

	def setUp(self) -> None:
		super().setUp()
		reset_numbering_state()
		frappe.local.db.commit()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def _scheme_payload(self) -> dict[str, object]:
		return {
			"code": "COD023-HTTP",
			"display_name": "HTTP编号方案",
			"company": TEST_COMPANY,
			"hospital": TEST_HOSPITAL,
			"template": "HTTP-{YYYY}{MM}-{DIM:department}-{SEQ}",
			"reset_policy": "Monthly",
			"sequence_digits": 4,
			"valid_from": "2026-01-01",
			"expected_revision": 0,
		}

	def test_http_write_requires_idempotency_header(self) -> None:
		response = self.post(
			self.method(UPSERT_SCHEME_METHOD),
			self._scheme_payload(),
			headers={"X-Correlation-ID": "COD-023-http-missing-key"},
		)
		self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
		self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0003")

	def test_http_create_allocate_replay_and_query(self) -> None:
		created_response = self.post(
			self.method(UPSERT_SCHEME_METHOD),
			self._scheme_payload(),
			headers={
				"Idempotency-Key": request_key("http", "scheme"),
				"X-Correlation-ID": "COD-023-http-scheme",
			},
		)
		self.assertEqual(created_response.status_code, 200, created_response.get_data(as_text=True))

		request = {
			"scheme": "COD023-HTTP",
			"dimensions": {"department": "FIN"},
			"date": "2026-08-08",
		}
		headers = {
			"Idempotency-Key": request_key("http", "number"),
			"X-Correlation-ID": "COD-023-http-number",
		}
		first_response = self.post(self.method(NEXT_NUMBER_METHOD), request, headers=headers)
		replay_response = self.post(self.method(NEXT_NUMBER_METHOD), request, headers=headers)
		self.assertEqual(first_response.status_code, 200, first_response.get_data(as_text=True))
		self.assertEqual(replay_response.status_code, 200, replay_response.get_data(as_text=True))
		first = first_response.get_json()["message"]
		replay = replay_response.get_json()["message"]
		self.assertEqual(first["number"], "HTTP-202608-FIN-0001")
		self.assertEqual(first["reservation_token"], replay["reservation_token"])
		self.assertTrue(replay["idempotency_replayed"])

		query_response = self.get(
			self.method(GET_RESERVATION_METHOD),
			{"reservation_token": first["reservation_token"]},
			headers={"X-Correlation-ID": "COD-023-http-query"},
		)
		self.assertEqual(query_response.status_code, 200, query_response.get_data(as_text=True))
		self.assertEqual(query_response.get_json()["message"]["number"], first["number"])

	def test_http_guest_is_rejected_before_numbering_lookup(self) -> None:
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		with patch("medops.api.v1.core.numbering.build_number_allocation") as builder:
			response = self.post(
				self.method(NEXT_NUMBER_METHOD),
				{"scheme": "MISSING", "dimensions": {}, "date": "2026-08-08"},
				headers={"Idempotency-Key": request_key("http", "guest")},
			)
		self.assertEqual(response.status_code, 401, response.get_data(as_text=True))
		self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0001")
		builder.assert_not_called()
