from __future__ import annotations

import unittest
from decimal import Decimal

from medops.common.immutable_ledger import (
	BASE_LEDGER_FIELDS,
	IMMUTABLE_LEDGER_SCHEMA_VERSION,
	ImmutableLedgerContractError,
	ImmutableLedgerDefinition,
	assert_reversal_matches,
	build_reversal_values,
	get_immutable_ledger_public_contract,
	normalize_dimensions_json,
	normalize_ledger_values,
)


def definition() -> ImmutableLedgerDefinition:
	return ImmutableLedgerDefinition(
		doctype="HRP Test Immutable Ledger",
		required_roles=frozenset({"System Manager"}),
		negated_fields=("quantity", "amount"),
		swapped_field_pairs=(("debit", "credit"),),
		reversal_override_fields=frozenset(
			{
				"posting_date",
				"posting_time",
				"voucher_type",
				"voucher_no",
				"source_hash",
			}
		),
		required_reversal_override_fields=frozenset(
			{"posting_date", "posting_time", "voucher_type", "voucher_no"}
		),
	)


class TestImmutableLedgerContract(unittest.TestCase):
	def test_definition_is_immutable_and_public_contract_discloses_no_roles(self) -> None:
		ledger = definition()

		self.assertEqual(ledger.as_public_dict()["schema_version"], IMMUTABLE_LEDGER_SCHEMA_VERSION)
		self.assertTrue(ledger.as_public_dict()["append_only"])
		self.assertNotIn("required_roles", ledger.as_public_dict())
		with self.assertRaisesRegex(AttributeError, "FrozenInstanceError|cannot assign"):
			ledger.doctype = "HRP Changed Ledger"  # type: ignore[misc]

	def test_definition_rejects_invalid_or_ambiguous_reversal_rules(self) -> None:
		defaults = {
			"doctype": "HRP Test Immutable Ledger",
			"required_roles": frozenset({"System Manager"}),
			"negated_fields": ("amount",),
			"swapped_field_pairs": (),
			"reversal_override_fields": frozenset({"voucher_no"}),
			"required_reversal_override_fields": frozenset({"voucher_no"}),
		}
		cases = (
			{"doctype": "Unsafe Ledger"},
			{"required_roles": frozenset()},
			{"negated_fields": ()},
			{"negated_fields": ("amount", "amount")},
			{"swapped_field_pairs": (("debit", "debit"),)},
			{"negated_fields": ("debit",), "swapped_field_pairs": (("debit", "credit"),)},
			{"reversal_override_fields": frozenset({"is_reversal"})},
			{
				"reversal_override_fields": frozenset(),
				"required_reversal_override_fields": frozenset({"voucher_no"}),
			},
		)
		for changes in cases:
			with self.subTest(changes=changes), self.assertRaises(ImmutableLedgerContractError):
				ImmutableLedgerDefinition(**{**defaults, **changes})

	def test_dimensions_are_canonical_and_must_be_an_object(self) -> None:
		self.assertEqual(
			normalize_dimensions_json('{"ward":"A","cost_center":"C01"}'),
			'{"cost_center":"C01","ward":"A"}',
		)
		self.assertEqual(normalize_dimensions_json(None), "")
		for value in ('["not","an","object"]', "{broken", 42):
			with self.subTest(value=value), self.assertRaises(ImmutableLedgerContractError):
				normalize_dimensions_json(value)

	def test_values_reject_reserved_unknown_and_unsupported_fields(self) -> None:
		allowed = frozenset({"amount", "posting_date", "dimensions_json"})
		self.assertEqual(
			normalize_ledger_values(
				{
					"amount": Decimal("12.30"),
					"posting_date": "2026-07-30",
					"dimensions_json": {"ward": "A"},
				},
				allowed_fields=allowed,
			),
			{
				"amount": "12.30",
				"posting_date": "2026-07-30",
				"dimensions_json": '{"ward":"A"}',
			},
		)
		for values in (
			{"name": "forced"},
			{"unknown": "value"},
			{"amount": object()},
		):
			with self.subTest(values=values), self.assertRaises(ImmutableLedgerContractError):
				normalize_ledger_values(values, allowed_fields=allowed)

	def test_reversal_is_equal_opposite_and_swaps_debit_credit(self) -> None:
		ledger = definition()
		original = {
			"name": "LEDGER-0001",
			"company": "Hospital",
			"quantity": Decimal("2.500000"),
			"debit": Decimal("80.00"),
			"credit": Decimal("20.00"),
			"amount": Decimal("60.00"),
			"dimensions_json": '{"ward":"A"}',
			"source_hash": "a" * 64,
		}
		fieldnames = frozenset(
			{
				"company",
				"posting_date",
				"posting_time",
				"voucher_type",
				"voucher_no",
				"quantity",
				"debit",
				"credit",
				"amount",
				"is_reversal",
				"reversal_of",
				"dimensions_json",
				"source_hash",
			}
		)

		reversal = build_reversal_values(
			original,
			definition=ledger,
			fieldnames=fieldnames,
			overrides={
				"posting_date": "2026-07-30",
				"posting_time": "12:00:00",
				"voucher_type": "Journal Entry",
				"voucher_no": "JV-0001",
				"source_hash": "b" * 64,
			},
		)

		self.assertEqual(reversal["quantity"], Decimal("-2.500000"))
		self.assertEqual(reversal["amount"], Decimal("-60.00"))
		self.assertEqual(reversal["debit"], Decimal("20.00"))
		self.assertEqual(reversal["credit"], Decimal("80.00"))
		self.assertEqual(reversal["reversal_of"], "LEDGER-0001")
		self.assertEqual(reversal["is_reversal"], 1)
		assert_reversal_matches(original, reversal, definition=ledger)

	def test_reversal_requires_context_and_rejects_tampering(self) -> None:
		ledger = definition()
		original = {
			"name": "LEDGER-0002",
			"quantity": 2,
			"debit": 30,
			"credit": 10,
			"amount": 20,
		}
		with self.assertRaisesRegex(ImmutableLedgerContractError, "required reversal override"):
			build_reversal_values(
				original,
				definition=ledger,
				fieldnames=frozenset(
					{
						"quantity",
						"debit",
						"credit",
						"amount",
						"is_reversal",
						"reversal_of",
					}
				),
				overrides={},
			)

		reversal = {
			"is_reversal": 1,
			"reversal_of": "LEDGER-0002",
			"quantity": -2,
			"debit": 10,
			"credit": 30,
			"amount": -19,
		}
		with self.assertRaisesRegex(ImmutableLedgerContractError, "equal and opposite"):
			assert_reversal_matches(original, reversal, definition=ledger)

	def test_public_contract_is_read_only_and_covers_all_base_fields(self) -> None:
		contract = get_immutable_ledger_public_contract()

		self.assertFalse(contract["http_write_enabled"])
		self.assertTrue(contract["mutation_policy"]["append_only"])
		self.assertTrue(contract["reversal_policy"]["row_lock"])
		self.assertEqual(
			{row["fieldname"] for row in contract["base_fields"]},
			{field.fieldname for field in BASE_LEDGER_FIELDS},
		)


if __name__ == "__main__":
	unittest.main()
