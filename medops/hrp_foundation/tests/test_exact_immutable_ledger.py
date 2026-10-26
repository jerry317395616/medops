from __future__ import annotations

from decimal import localcontext
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.domain_service import DomainServiceDefinition, idempotency_record_name
from medops.common.error_catalog import IoneApplicationError
from medops.common.immutable_ledger import ImmutableLedgerContractError, ImmutableLedgerDefinition
from medops.hrp_foundation.tests import test_immutable_ledger as legacy
from medops.services.audit_context import service_audit_scope
from medops.services.immutable_ledger import (
	AppendImmutableLedgerService,
	AppendLedgerEntry,
	ImmutableLedgerDocument,
	ReverseImmutableLedgerService,
	ReverseLedgerEntry,
	append_immutable_entry,
	validate_immutable_ledger_doctype,
)

TEST_DOCTYPE = "HRP Exact Test Ledger"
EXACT_DEFINITION = ImmutableLedgerDefinition(
	doctype=TEST_DOCTYPE,
	required_roles=frozenset({"System Manager"}),
	negated_fields=("amount",),
	swapped_field_pairs=(("debit", "credit"),),
	reversal_override_fields=legacy.TEST_LEDGER_DEFINITION.reversal_override_fields,
	required_reversal_override_fields=legacy.TEST_LEDGER_DEFINITION.required_reversal_override_fields,
	amount_schema_version=2,
)


class ExactLedgerEntry(ImmutableLedgerDocument):
	ledger_definition = EXACT_DEFINITION


class AppendExactLedger(AppendImmutableLedgerService):
	ledger_definition = EXACT_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_foundation.exact_test_ledger.append",
		version=2,
		kind="command",
		required_roles=EXACT_DEFINITION.required_roles,
	)


class ReverseExactLedger(ReverseImmutableLedgerService):
	ledger_definition = EXACT_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_foundation.exact_test_ledger.reverse",
		version=2,
		kind="command",
		required_roles=EXACT_DEFINITION.required_roles,
	)


def _doctype_payload():
	payload: dict[str, Any] = legacy.ledger_test_doctype_payload()
	payload["name"] = TEST_DOCTYPE
	for field in payload["fields"]:
		if field["fieldname"] in {"amount", "debit", "credit"}:
			field.update(fieldtype="Data", reqd=1)
			field.pop("options", None)
			field.pop("precision", None)
		if field["fieldname"] == "reversal_of":
			field["options"] = TEST_DOCTYPE
	payload["fields"] += [
		dict(fieldname="amount_schema_version", label="Amount Schema Version", fieldtype="Int", reqd=1),
		dict(fieldname="currency_precision", label="Currency Precision", fieldtype="Int", reqd=1),
		dict(fieldname="project", label="Project", fieldtype="Data", reqd=1),
	]
	return payload


def _values(precision=9) -> dict[str, Any]:
	value = "999999999999999" + ("." + "0" * (precision - 1) + "1" if precision else "")
	return dict(
		legacy.ledger_entry_values(),
		amount=value,
		debit=value,
		credit="0" + ("." + "0" * precision if precision else ""),
		currency="CNY",
		amount_schema_version=2,
		currency_precision=precision,
		project="EXACT-PROJECT",
	)


def _key():
	return f"cod033-exact-{uuid4().hex}"


class TestExactImmutableLedger(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._drop_test_doctype()
		frappe.get_doc(_doctype_payload()).insert(ignore_permissions=True)
		frappe.local.db.commit()
		frappe.controllers.setdefault(frappe.local.site, {})[TEST_DOCTYPE] = ExactLedgerEntry
		cls.addClassCleanup(cls._drop_test_doctype)

	@classmethod
	def _drop_test_doctype(cls):
		frappe.controllers.setdefault(frappe.local.site, {}).pop(TEST_DOCTYPE, None)
		for service in (AppendExactLedger, ReverseExactLedger):
			frappe.db.delete("HRP Service Idempotency", {"service_name": service.definition.name})
		if frappe.db.exists("DocType", TEST_DOCTYPE):
			frappe.db.delete(TEST_DOCTYPE)
			frappe.delete_doc(
				"DocType", TEST_DOCTYPE, ignore_permissions=True, force=True, delete_permanently=True
			)
		frappe.local.db.commit()

	def _append(self, values=None, key=None) -> dict[str, Any]:
		return (
			AppendExactLedger()
			.execute(
				AppendLedgerEntry(_values() if values is None else values), idempotency_key=key or _key()
			)
			.result
		)

	def test_all_captured_precisions_round_trip_without_database_rounding(self):
		for precision in range(10):
			with self.subTest(precision=precision):
				values = _values(precision)
				entry = frappe.get_doc(TEST_DOCTYPE, self._append(values)["entry"])
				for field in ("amount", "debit", "credit", "currency_precision", "amount_schema_version"):
					self.assertEqual(entry.get(field), values[field])
				self.assertIsInstance(entry.amount, str)

	def test_reversal_is_exact_idempotent_and_preserves_original_profile(self):
		values = _values()
		entry = self._append(values)["entry"]
		key = _key()
		command = ReverseLedgerEntry(entry, legacy.ledger_reversal_overrides(key))
		with localcontext() as context, patch("frappe.defaults.get_global_default", return_value=2):
			context.prec = 12
			first = ReverseExactLedger().execute(command, idempotency_key=key)
			second = ReverseExactLedger().execute(command, idempotency_key=key)
		self.assertFalse(first.idempotency_replayed)
		self.assertTrue(second.idempotency_replayed)
		self.assertEqual(first.result, second.result)
		reversal = frappe.get_doc(TEST_DOCTYPE, first.result["entry"])
		self.assertEqual(reversal.amount, "-" + values["amount"])
		self.assertEqual(reversal.debit, values["credit"])
		self.assertEqual(reversal.credit, values["debit"])
		self.assertEqual(reversal.currency_precision, 9)
		self.assertEqual(reversal.amount_schema_version, 2)
		self.assertEqual(reversal.project, values["project"])
		self.assertEqual(frappe.db.count(TEST_DOCTYPE, {"reversal_of": entry}), 1)

	def test_invalid_profiles_and_floats_fail_before_reservation_or_rows(self):
		before = frappe.db.count(TEST_DOCTYPE)
		for changes in (
			{"amount": 0.1},
			{"debit": 0.1},
			{"amount": "0.0000000001"},
			{"amount_schema_version": "2"},
			{"currency_precision": True},
			{"currency_precision": 10},
			{"amount": "NaN"},
		):
			key = _key()
			with self.subTest(changes=changes), self.assertRaises(IoneApplicationError) as raised:
				self._append(dict(_values(), **changes), key)
			self.assertEqual(raised.exception.code, "IONE-CORE-0003")
			self.assertFalse(
				frappe.db.exists(
					"HRP Service Idempotency", idempotency_record_name(AppendExactLedger.definition.name, key)
				)
			)
		self.assertEqual(frappe.db.count(TEST_DOCTYPE), before)

	def test_controller_reversal_grain_tampering_rolls_back_the_entire_command(self):
		entry = self._append()["entry"]
		original_before_validate = ExactLedgerEntry.before_validate

		def tamper(document):
			original_before_validate(document)
			document.project = "OTHER-PROJECT"

		key = _key()
		with (
			patch.object(ExactLedgerEntry, "before_validate", tamper),
			self.assertRaises(IoneApplicationError),
		):
			ReverseExactLedger().execute(
				ReverseLedgerEntry(entry, legacy.ledger_reversal_overrides(key)), idempotency_key=key
			)
		self.assertFalse(frappe.db.get_value(TEST_DOCTYPE, {"reversal_of": entry}, "name"))
		self.assertFalse(
			frappe.db.exists(
				"HRP Service Idempotency", idempotency_record_name(ReverseExactLedger.definition.name, key)
			)
		)

	def test_native_mutation_and_inexact_metadata_are_rejected(self):
		with self.assertRaises(IoneApplicationError):
			frappe.get_doc(dict(doctype=TEST_DOCTYPE, **_values())).insert(ignore_permissions=True)
		entry = frappe.get_doc(TEST_DOCTYPE, self._append()["entry"])
		for mutation in (
			lambda: entry.db_set("amount", "1.000000000"),
			lambda: frappe.delete_doc(TEST_DOCTYPE, entry.name, ignore_permissions=True, force=True),
		):
			with self.assertRaises(IoneApplicationError):
				mutation()
		meta = frappe.get_meta(TEST_DOCTYPE)
		with patch.object(meta.get_field("amount"), "fieldtype", "Currency"):
			with self.assertRaises(IoneApplicationError) as raised:
				validate_immutable_ledger_doctype(EXACT_DEFINITION)
			self.assertEqual(raised.exception.code, "IONE-CORE-0009")
		self.assertEqual(frappe.db.get_value(TEST_DOCTYPE, entry.name, "amount"), _values()["amount"])

	def test_batch_helper_obeys_outer_savepoint_without_commit(self):
		before = frappe.db.count(TEST_DOCTYPE)
		frappe.db.savepoint("cod033_exact_batch")
		try:
			with service_audit_scope():
				append_immutable_entry(EXACT_DEFINITION, _values())
				with self.assertRaises(ImmutableLedgerContractError):
					append_immutable_entry(EXACT_DEFINITION, dict(_values(), amount=0.1))
		finally:
			frappe.db.rollback(save_point="cod033_exact_batch")
		self.assertEqual(frappe.db.count(TEST_DOCTYPE), before)
