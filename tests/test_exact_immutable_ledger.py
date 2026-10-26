from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

from medops.common.exact_money import ExactMoneyError, signed_decimal_amount
from medops.common.immutable_ledger import (
	BASE_LEDGER_FIELDS,
	ImmutableLedgerContractError,
	ImmutableLedgerDefinition,
	assert_reversal_matches,
	build_reversal_values,
	get_immutable_ledger_public_contract,
	ledger_field_contracts,
	normalize_exact_ledger_money,
	normalize_ledger_values,
)


def definition():
	return ImmutableLedgerDefinition(
		doctype="HRP Exact Money Ledger",
		required_roles=frozenset({"System Manager"}),
		negated_fields=("amount",),
		swapped_field_pairs=(("debit", "credit"),),
		reversal_override_fields=frozenset({"voucher_no", "posting_date", "source_hash"}),
		amount_schema_version=2,
	)


def original():
	return dict(
		name="ORIGINAL",
		company="Company",
		organization_unit="Ward-A",
		currency="CNY",
		amount_schema_version=2,
		currency_precision=9,
		amount="999999999999999.000000001",
		debit="999999999999999.000000001",
		credit="0.000000000",
		voucher_no="VOUCHER",
		posting_date=date(2026, 10, 26),
		source_hash="a" * 64,
		dimensions_json='{"project":"P1","year":"2026"}',
		hospital="Hospital",
		project="P1",
		period_start=date(2026, 1, 1),
		period_end=date(2026, 1, 31),
	)


def reversal(data=None, overrides=None):
	data = original() if data is None else data
	return build_reversal_values(
		data,
		definition=definition(),
		fieldnames=frozenset(data) - {"name"} | {"is_reversal", "reversal_of"},
		overrides=overrides or {},
	)


class TestExactImmutableLedger(unittest.TestCase):
	def test_legacy_public_contract_and_definition_are_frozen(self):
		frozen = json.loads(
			(Path(__file__).parent / "fixtures/immutable_ledger_v1_contract.json").read_text(encoding="utf-8")
		)
		legacy = ImmutableLedgerDefinition(
			doctype="HRP Legacy Exactness Ledger",
			required_roles=frozenset({"System Manager"}),
			negated_fields=("amount",),
			swapped_field_pairs=(("debit", "credit"),),
		)
		self.assertEqual(get_immutable_ledger_public_contract(), frozen["public_contract"])
		self.assertEqual(legacy.as_public_dict(), frozen["definition"])
		self.assertIs(ledger_field_contracts(legacy), BASE_LEDGER_FIELDS)
		self.assertEqual(normalize_ledger_values({"amount": 0.1}, definition=legacy), {"amount": 0.1})

	def test_exact_schema_is_explicit_and_metadata_has_required_data_amounts(self):
		contracts = {field.fieldname: field for field in ledger_field_contracts(definition())}
		for name in ("amount", "debit", "credit"):
			self.assertEqual(contracts[name].fieldtype, "Data")
			self.assertTrue(contracts[name].required)
		for name in ("currency_precision", "amount_schema_version"):
			self.assertEqual(contracts[name].fieldtype, "Int")
			self.assertTrue(contracts[name].required)
		self.assertEqual(definition().as_public_dict()["schema_version"], 2)
		for version in (0, 3, True, 2.0, "2"):
			with self.subTest(version=version), self.assertRaises(ImmutableLedgerContractError):
				replace(definition(), amount_schema_version=version)

	def test_exact_reversal_cannot_override_money_profile_or_dimensions(self):
		for key in (
			"currency",
			"currency_precision",
			"amount_schema_version",
			"company",
			"organization_unit",
			"dimensions_json",
		):
			with self.subTest(key=key), self.assertRaises(ImmutableLedgerContractError):
				replace(definition(), reversal_override_fields=frozenset({key}))

	def test_signed_amounts_keep_every_captured_precision(self):
		for precision in range(10):
			value = "123456789012345" + ("." + "0" * (precision - 1) + "1" if precision else "")
			with self.subTest(precision=precision):
				self.assertEqual(signed_decimal_amount(value, precision), value)
				self.assertEqual(signed_decimal_amount("-" + value, precision), "-" + value)

	def test_binary_floats_nonfinite_overflow_and_excess_fraction_are_rejected(self):
		for value in (
			True,
			0.1,
			float("nan"),
			"NaN",
			"Infinity",
			"-Infinity",
			"1000000000000000",
			"-1000000000000000",
			"0.0000000001",
			"-0.0000000001",
		):
			with self.subTest(value=value), self.assertRaises(ExactMoneyError):
				signed_decimal_amount(value, 9)
		for key in ("amount", "debit", "credit"):
			with self.subTest(key=key), self.assertRaises(ImmutableLedgerContractError):
				normalize_exact_ledger_money({**original(), key: 0.1})

	def test_profile_is_required_without_global_currency_fallback(self):
		for key, value in (
			("amount_schema_version", None),
			("amount_schema_version", 1),
			("amount_schema_version", True),
			("currency_precision", None),
			("currency_precision", True),
			("currency_precision", 10),
			("currency_precision", 2.0),
		):
			with self.subTest(key=key, value=value), self.assertRaises(ImmutableLedgerContractError):
				normalize_exact_ledger_money({**original(), key: value})

	def test_debit_and_credit_cannot_be_negative_or_missing(self):
		for key in ("debit", "credit"):
			for value in ("-0.000000001", None):
				with self.subTest(key=key, value=value), self.assertRaises(ImmutableLedgerContractError):
					normalize_exact_ledger_money({**original(), key: value})

	def test_signed_zero_and_equivalent_wire_values_have_one_canonical_form(self):
		data = normalize_exact_ledger_money({**original(), "amount": "-0", "debit": "-0.00", "credit": "0"})
		for key in ("amount", "debit", "credit"):
			self.assertEqual(data[key], "0.000000000")
		self.assertEqual(signed_decimal_amount(Decimal("-0"), 0), "0")
		self.assertEqual(
			normalize_ledger_values(
				{
					"amount_schema_version": 2,
					"currency_precision": 2,
					"amount": Decimal("1"),
					"debit": "1.0",
					"credit": 0,
				},
				definition=definition(),
			)["amount"],
			"1.00",
		)

	def test_reversal_is_exact_even_under_a_callers_small_decimal_context(self):
		with localcontext() as context:
			context.prec = 6
			result = reversal()
			self.assertEqual(result["amount"], "-999999999999999.000000001")
			self.assertEqual(result["debit"], "0.000000000")
			self.assertEqual(result["credit"], "999999999999999.000000001")
			assert_reversal_matches(original(), result, definition=definition())

	def test_reversal_preserves_each_business_grain_and_date(self):
		result = reversal()
		assert_reversal_matches(original(), result, definition=definition())
		for key in ("company", "organization_unit", "currency", "hospital", "project", "dimensions_json"):
			self.assertEqual(result[key], original()[key])
		self.assertEqual(result["period_start"], original()["period_start"])

	def test_reversal_rejects_scope_period_and_source_context_tampering(self):
		result = reversal()
		for key in (
			"company",
			"organization_unit",
			"currency",
			"hospital",
			"project",
			"period_start",
			"period_end",
			"dimensions_json",
		):
			with self.subTest(key=key), self.assertRaises(ImmutableLedgerContractError):
				assert_reversal_matches(original(), {**result, key: "Other"}, definition=definition())

	def test_reversal_rejects_changed_precision_float_and_wrong_opposite(self):
		for changes in (
			{"currency_precision": 8},
			{"amount": -0.1},
			{"amount": "-999999999999999.000000002"},
			{"amount_schema_version": 1},
		):
			with self.subTest(changes=changes), self.assertRaises(ImmutableLedgerContractError):
				assert_reversal_matches(original(), {**reversal(), **changes}, definition=definition())

	def test_only_declared_operational_reversal_overrides_change(self):
		result = reversal(
			overrides={"voucher_no": "REVERSAL", "posting_date": "2026-10-27", "source_hash": "b" * 64}
		)
		self.assertEqual(result["voucher_no"], "REVERSAL")
		assert_reversal_matches(original(), result, definition=definition())
		with self.assertRaises(ImmutableLedgerContractError):
			reversal(overrides={"project": "P2"})

	def test_original_precision_is_not_requantized_by_current_settings(self):
		result = reversal()
		self.assertEqual(result["currency_precision"], 9)
		self.assertEqual(result["amount_schema_version"], 2)
		self.assertEqual(result["reversal_of"], "ORIGINAL")
		for key in ("debit", "credit", "amount"):
			self.assertIsInstance(result[key], str)
