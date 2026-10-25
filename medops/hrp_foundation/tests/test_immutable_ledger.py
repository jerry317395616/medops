from __future__ import annotations

from hashlib import sha256
from typing import Any
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase
from frappe.utils import nowdate, nowtime

from medops.common.domain_service import (
	DomainServiceDefinition,
	idempotency_record_name,
)
from medops.common.error_catalog import IoneApplicationError
from medops.common.immutable_ledger import ImmutableLedgerDefinition
from medops.services.immutable_ledger import (
	AppendImmutableLedgerService,
	AppendLedgerEntry,
	ImmutableLedgerDocument,
	ReverseImmutableLedgerService,
	ReverseLedgerEntry,
	get_immutable_ledger_contract_status,
)

TEST_DOCTYPE = "HRP Test Immutable Ledger"
LEDGER_METHOD = "medops.api.v1.ledgers.get_immutable_ledger_contract"
TEST_LEDGER_DEFINITION = ImmutableLedgerDefinition(
	doctype=TEST_DOCTYPE,
	required_roles=frozenset({"System Manager"}),
	negated_fields=("quantity", "amount"),
	swapped_field_pairs=(("debit", "credit"),),
	reversal_override_fields=frozenset(
		{
			"posting_date",
			"posting_time",
			"voucher_type",
			"voucher_no",
			"reference_type",
			"reference_name",
			"source_hash",
		}
	),
	required_reversal_override_fields=frozenset(
		{"posting_date", "posting_time", "voucher_type", "voucher_no"}
	),
)


class TestImmutableLedgerEntry(ImmutableLedgerDocument):
	ledger_definition = TEST_LEDGER_DEFINITION


class AppendTestLedgerService(AppendImmutableLedgerService):
	ledger_definition = TEST_LEDGER_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_foundation.test_ledger.append",
		version=1,
		kind="command",
		required_roles=TEST_LEDGER_DEFINITION.required_roles,
	)


class ReverseTestLedgerService(ReverseImmutableLedgerService):
	ledger_definition = TEST_LEDGER_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_foundation.test_ledger.reverse",
		version=1,
		kind="command",
		required_roles=TEST_LEDGER_DEFINITION.required_roles,
	)


def _test_doctype_payload() -> dict[str, object]:
	return {
		"doctype": "DocType",
		"name": TEST_DOCTYPE,
		"module": "Custom",
		"custom": 1,
		"engine": "InnoDB",
		"autoname": "hash",
		"allow_rename": 0,
		"is_submittable": 0,
		"issingle": 0,
		"istable": 0,
		"track_changes": 0,
		"fields": [
			{
				"fieldname": "company",
				"label": "Company",
				"fieldtype": "Link",
				"options": "User",
				"reqd": 1,
			},
			{
				"fieldname": "organization_unit",
				"label": "Organization Unit",
				"fieldtype": "Link",
				"options": "User",
				"reqd": 1,
			},
			{
				"fieldname": "posting_date",
				"label": "Posting Date",
				"fieldtype": "Date",
				"reqd": 1,
			},
			{
				"fieldname": "posting_time",
				"label": "Posting Time",
				"fieldtype": "Time",
				"reqd": 1,
			},
			{
				"fieldname": "voucher_type",
				"label": "Voucher Type",
				"fieldtype": "Link",
				"options": "DocType",
				"reqd": 1,
			},
			{
				"fieldname": "voucher_no",
				"label": "Voucher No",
				"fieldtype": "Dynamic Link",
				"options": "voucher_type",
				"reqd": 1,
			},
			{
				"fieldname": "reference_type",
				"label": "Reference Type",
				"fieldtype": "Link",
				"options": "DocType",
			},
			{
				"fieldname": "reference_name",
				"label": "Reference Name",
				"fieldtype": "Dynamic Link",
				"options": "reference_type",
			},
			{
				"fieldname": "quantity",
				"label": "Quantity",
				"fieldtype": "Float",
				"precision": "6",
			},
			{
				"fieldname": "debit",
				"label": "Debit",
				"fieldtype": "Currency",
				"options": "currency",
				"precision": "2",
			},
			{
				"fieldname": "credit",
				"label": "Credit",
				"fieldtype": "Currency",
				"options": "currency",
				"precision": "2",
			},
			{
				"fieldname": "amount",
				"label": "Amount",
				"fieldtype": "Currency",
				"options": "currency",
				"precision": "2",
			},
			{
				"fieldname": "currency",
				"label": "Currency",
				"fieldtype": "Link",
				"options": "Currency",
			},
			{
				"fieldname": "is_reversal",
				"label": "Is Reversal",
				"fieldtype": "Check",
				"default": "0",
			},
			{
				"fieldname": "reversal_of",
				"label": "Reversal Of",
				"fieldtype": "Link",
				"options": TEST_DOCTYPE,
			},
			{
				"fieldname": "dimensions_json",
				"label": "Dimensions",
				"fieldtype": "Code",
				"options": "JSON",
			},
			{
				"fieldname": "source_hash",
				"label": "Source Hash",
				"fieldtype": "Data",
			},
		],
		"permissions": [
			{
				"role": "System Manager",
				"read": 1,
				"write": 0,
				"create": 0,
				"delete": 0,
				"submit": 0,
				"cancel": 0,
				"amend": 0,
				"import": 0,
				"report": 1,
				"select": 1,
			}
		],
	}


def _entry_values(*, amount: float = 60.0) -> dict[str, object]:
	return {
		"company": "Administrator",
		"organization_unit": "Administrator",
		"posting_date": nowdate(),
		"posting_time": nowtime(),
		"voucher_type": "User",
		"voucher_no": "Administrator",
		"reference_type": "User",
		"reference_name": "Administrator",
		"quantity": 2.5,
		"debit": 80.0,
		"credit": 20.0,
		"amount": amount,
		"dimensions_json": '{"ward":"A","cost_center":"C01"}',
		"source_hash": sha256(f"source-{amount}".encode()).hexdigest(),
	}


def _reversal_overrides(label: str) -> dict[str, object]:
	return {
		"posting_date": nowdate(),
		"posting_time": nowtime(),
		"voucher_type": "User",
		"voucher_no": "Administrator",
		"reference_type": "User",
		"reference_name": "Administrator",
		"source_hash": sha256(label.encode()).hexdigest(),
	}


def _test_idempotency_key(label: str) -> str:
	return "-".join(("cod012", label, "test"))


class TestImmutableLedger(IntegrationTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		if frappe.db.exists("DocType", TEST_DOCTYPE):
			frappe.db.delete(TEST_DOCTYPE)
			frappe.delete_doc(
				"DocType",
				TEST_DOCTYPE,
				ignore_permissions=True,
				force=True,
				delete_permanently=True,
			)
		frappe.get_doc(_test_doctype_payload()).insert(ignore_permissions=True)
		frappe.local.db.commit()
		frappe.controllers.setdefault(frappe.local.site, {})[TEST_DOCTYPE] = TestImmutableLedgerEntry
		cls.addClassCleanup(cls._drop_test_doctype)

	@classmethod
	def _drop_test_doctype(cls) -> None:
		del cls
		frappe.controllers.setdefault(frappe.local.site, {}).pop(TEST_DOCTYPE, None)
		for service_name in (
			AppendTestLedgerService.definition.name,
			ReverseTestLedgerService.definition.name,
		):
			frappe.db.delete("HRP Service Idempotency", {"service_name": service_name})
		if frappe.db.exists("DocType", TEST_DOCTYPE):
			frappe.db.delete(TEST_DOCTYPE)
			frappe.delete_doc(
				"DocType",
				TEST_DOCTYPE,
				ignore_permissions=True,
				force=True,
				delete_permanently=True,
			)
		frappe.local.db.commit()

	def _append(
		self,
		key: str,
		*,
		amount: float = 60.0,
		values: dict[str, object] | None = None,
	) -> dict[str, Any]:
		command_values = _entry_values(amount=amount) if values is None else values
		return (
			AppendTestLedgerService()
			.execute(
				AppendLedgerEntry(command_values),
				idempotency_key=key,
				correlation_id=key,
			)
			.as_public_dict()
		)

	def _reverse(self, entry_name: str, key: str) -> dict[str, Any]:
		return (
			ReverseTestLedgerService()
			.execute(
				ReverseLedgerEntry(entry_name, _reversal_overrides(key)),
				idempotency_key=key,
				correlation_id=key,
			)
			.as_public_dict()
		)

	def test_append_is_service_only_idempotent_and_audited(self) -> None:
		key = _test_idempotency_key("append-idempotent")
		values = _entry_values()
		with patch("medops.services.audit_context.frappe.logger") as logger:
			first = self._append(key, values=values)
			second = self._append(key, values=values)

		self.assertFalse(first["idempotency_replayed"])
		self.assertTrue(second["idempotency_replayed"])
		self.assertEqual(first["result"]["entry"], second["result"]["entry"])
		self.assertEqual(
			frappe.db.count(
				TEST_DOCTYPE,
				{"name": first["result"]["entry"]},
			),
			1,
		)
		events = [call.args[0]["event"] for call in logger.return_value.info.call_args_list]
		self.assertIn("immutable_ledger_appended", events)
		self.assertIn("domain_service_replayed", events)
		self.assertNotIn(first["result"]["entry"], str(events))

	def test_direct_insert_update_db_set_delete_and_rename_are_rejected(self) -> None:
		direct = frappe.get_doc({"doctype": TEST_DOCTYPE, **_entry_values()})
		with self.assertRaises(IoneApplicationError) as raised:
			direct.insert(ignore_permissions=True)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")

		entry_name = self._append("COD-012-immutable-0001")["result"]["entry"]
		document = frappe.get_doc(TEST_DOCTYPE, entry_name)
		document.amount = 999
		with self.assertRaises(IoneApplicationError) as raised:
			document.save(ignore_permissions=True)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")

		with self.assertRaises(IoneApplicationError) as raised:
			document.db_set("amount", 999)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")

		with self.assertRaises(IoneApplicationError) as raised:
			frappe.delete_doc(TEST_DOCTYPE, entry_name, ignore_permissions=True, force=True)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")

		with self.assertRaises(IoneApplicationError) as raised:
			frappe.rename_doc(
				TEST_DOCTYPE,
				entry_name,
				f"{entry_name}-changed",
				force=True,
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")
		self.assertEqual(frappe.db.get_value(TEST_DOCTYPE, entry_name, "amount"), 60.0)

	def test_permission_is_checked_before_idempotency_reservation(self) -> None:
		key = _test_idempotency_key("permission")
		before = frappe.db.count("HRP Service Idempotency")
		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			self.assertRaises(IoneApplicationError) as raised,
		):
			self._append(key)

		self.assertEqual(raised.exception.code, "IONE-CORE-0002")
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)
		self.assertFalse(
			frappe.db.exists(
				"HRP Service Idempotency",
				idempotency_record_name(AppendTestLedgerService.definition.name, key),
			)
		)

	def test_reversal_is_equal_opposite_and_an_entry_can_only_be_reversed_once(self) -> None:
		entry_name = self._append("COD-012-reversal-source-0001")["result"]["entry"]
		reversal_result = self._reverse(entry_name, "COD-012-reversal-0001")
		reversal = frappe.get_doc(TEST_DOCTYPE, reversal_result["result"]["entry"])

		self.assertEqual(reversal.reversal_of, entry_name)
		self.assertEqual(reversal.is_reversal, 1)
		self.assertEqual(reversal.quantity, -2.5)
		self.assertEqual(reversal.amount, -60.0)
		self.assertEqual(reversal.debit, 20.0)
		self.assertEqual(reversal.credit, 80.0)
		self.assertEqual(reversal.dimensions_json, '{"cost_center":"C01","ward":"A"}')

		with self.assertRaises(IoneApplicationError) as raised:
			self._reverse(entry_name, "COD-012-reversal-duplicate-0001")
		self.assertEqual(raised.exception.code, "IONE-CORE-0006")

		with self.assertRaises(IoneApplicationError) as raised:
			self._reverse(reversal.name, "COD-012-reversal-chain-0001")
		self.assertEqual(raised.exception.code, "IONE-CORE-0006")
		self.assertEqual(frappe.db.count(TEST_DOCTYPE, {"reversal_of": entry_name}), 1)

	def test_lock_contention_fails_fast_and_rolls_back_idempotency(self) -> None:
		entry_name = self._append("COD-012-concurrency-source-0001")["result"]["entry"]
		self._primary_connection.commit()
		reversal_key = _test_idempotency_key("concurrency-reversal")
		reservation_name = idempotency_record_name(
			ReverseTestLedgerService.definition.name,
			reversal_key,
		)

		with self.primary_connection():
			self.assertEqual(
				frappe.db.get_value(TEST_DOCTYPE, entry_name, "name", for_update=True),
				entry_name,
			)
			with self.secondary_connection(), self.assertRaises(IoneApplicationError) as raised:
				self._reverse(entry_name, reversal_key)
			self.assertEqual(raised.exception.code, "IONE-CORE-0005")

		with self.secondary_connection():
			self.assertFalse(frappe.db.exists("HRP Service Idempotency", reservation_name))
			self.assertFalse(frappe.db.get_value(TEST_DOCTYPE, {"reversal_of": entry_name}, "name"))

		self._primary_connection.rollback()
		reversal = self._reverse(entry_name, "COD-012-concurrency-after-release-0001")
		self.assertEqual(reversal["result"]["reversal_of"], entry_name)
		self.assertEqual(frappe.db.count(TEST_DOCTYPE, {"reversal_of": entry_name}), 1)

	def test_invalid_hash_and_unknown_entry_fail_without_ledger_write(self) -> None:
		values = _entry_values()
		values["source_hash"] = "not-a-hash"
		before = frappe.db.count(TEST_DOCTYPE)
		with self.assertRaises(IoneApplicationError) as raised:
			AppendTestLedgerService().execute(
				AppendLedgerEntry(values),
				idempotency_key="COD-012-invalid-hash-0001",
				correlation_id="COD-012-invalid-hash-0001",
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0003")
		self.assertEqual(frappe.db.count(TEST_DOCTYPE), before)

		with self.assertRaises(IoneApplicationError) as raised:
			self._reverse("missing-ledger-entry", "COD-012-missing-entry-0001")
		self.assertEqual(raised.exception.code, "IONE-CORE-0004")

	def test_contract_status_requires_system_role_and_is_read_only(self) -> None:
		status = get_immutable_ledger_contract_status()
		self.assertEqual(status["schema_version"], 1)
		self.assertFalse(status["http_write_enabled"])
		self.assertTrue(status["mutation_policy"]["append_only"])

		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			self.assertRaises(IoneApplicationError) as raised,
		):
			get_immutable_ledger_contract_status()
		self.assertEqual(raised.exception.code, "IONE-CORE-0002")


class TestImmutableLedgerAPI(FrappeAPITestCase):
	def setUp(self) -> None:
		super().setUp()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def test_http_contract_is_read_only_and_returns_shared_schema(self) -> None:
		response = self.get(self.method(LEDGER_METHOD))

		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		payload = response.get_json()["message"]
		self.assertEqual(payload["schema_version"], 1)
		self.assertFalse(payload["http_write_enabled"])
		self.assertTrue(payload["reversal_policy"]["row_lock"])
		self.assertGreaterEqual(len(payload["base_fields"]), 17)

	def test_http_contract_rejects_guest(self) -> None:
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		response = self.get(self.method(LEDGER_METHOD))

		self.assertEqual(response.status_code, 401, response.get_data(as_text=True))
		self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0001")
