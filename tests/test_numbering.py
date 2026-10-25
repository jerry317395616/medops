from __future__ import annotations

import json
import unittest

from medops.common.numbering import (
	MAX_DIMENSIONS,
	NumberingContractError,
	build_number_allocation,
	build_numbering_scheme_upsert,
	counter_key_for,
	dimension_keys_for,
	dimensions_digest_for,
	normalize_dimensions,
	normalize_reservation_token,
	normalize_template,
	render_number,
	reservation_name_for,
	reset_bucket_for,
)


class NumberingContractTests(unittest.TestCase):
	def test_template_accepts_allowlisted_tokens_and_dimensions(self) -> None:
		template = normalize_template("PO-{YYYY}{MM}-{DIM:department}-{SEQ}")
		self.assertEqual(template, "PO-{YYYY}{MM}-{DIM:department}-{SEQ}")
		self.assertEqual(dimension_keys_for(template), ("department",))

	def test_template_rejects_missing_or_duplicate_sequence(self) -> None:
		for template in ("PO-{YYYY}", "PO-{SEQ}-{SEQ}"):
			with self.subTest(template=template), self.assertRaises(NumberingContractError):
				normalize_template(template)

	def test_template_rejects_expression_and_lowercase_literal(self) -> None:
		for template in (
			"PO-{__import__:os}-{SEQ}",
			"PO-{DIM:department.__class__}-{SEQ}",
			"po-{SEQ}",
			"PO {SEQ}",
		):
			with self.subTest(template=template), self.assertRaises(NumberingContractError):
				normalize_template(template)

	def test_template_rejects_duplicate_and_excess_dimensions(self) -> None:
		with self.assertRaises(NumberingContractError):
			dimension_keys_for(normalize_template("PO-{DIM:site}-{DIM:site}-{SEQ}"))
		with self.assertRaises(NumberingContractError):
			normalize_dimensions({f"d{index}": "X" for index in range(MAX_DIMENSIONS + 1)})

	def test_dimensions_are_canonical_and_bounded(self) -> None:
		normalized = normalize_dimensions(
			{"site": "xa-01", "department": "hr"},
			required_keys=("department", "site"),
		)
		self.assertEqual(normalized, {"department": "HR", "site": "XA-01"})
		self.assertEqual(
			dimensions_digest_for(normalized), dimensions_digest_for(dict(reversed(normalized.items())))
		)

	def test_dimensions_accept_json_and_require_exact_keys(self) -> None:
		self.assertEqual(
			normalize_dimensions('{"department":"FIN"}', required_keys=("department",)),
			{"department": "FIN"},
		)
		for payload in ({}, {"department": "FIN", "extra": "X"}, "[]", "not-json"):
			with self.subTest(payload=payload), self.assertRaises(NumberingContractError):
				normalize_dimensions(payload, required_keys=("department",))

	def test_dimensions_reject_non_scalar_and_injection_values(self) -> None:
		for payload in (
			{"department": ["FIN"]},
			{"department": "FIN/../../"},
			{"department": "FIN;DROP"},
			{"Department": "FIN"},
		):
			with self.subTest(payload=payload), self.assertRaises(NumberingContractError):
				normalize_dimensions(payload)

	def test_reset_buckets_are_deterministic(self) -> None:
		self.assertEqual(reset_bucket_for("Never", "2026-08-08"), "ALL")
		self.assertEqual(reset_bucket_for("Yearly", "2026-08-08"), "2026")
		self.assertEqual(reset_bucket_for("Monthly", "2026-08-08"), "202608")
		self.assertEqual(reset_bucket_for("Daily", "2026-08-08"), "20260808")

	def test_counter_key_is_stable_and_scope_specific(self) -> None:
		base = counter_key_for(
			scheme="PO",
			reset_policy="Monthly",
			business_date="2026-08-08",
			dimensions={"department": "FIN"},
		)
		self.assertEqual(
			base,
			counter_key_for(
				scheme="PO",
				reset_policy="Monthly",
				business_date="2026-08-31",
				dimensions={"department": "FIN"},
			),
		)
		self.assertNotEqual(
			base,
			counter_key_for(
				scheme="PO",
				reset_policy="Monthly",
				business_date="2026-09-01",
				dimensions={"department": "FIN"},
			),
		)

	def test_render_number_replaces_date_dimension_and_sequence(self) -> None:
		self.assertEqual(
			render_number(
				template="PO-{YY}{MM}{DD}-{DIM:department}-{SEQ}",
				business_date="2026-08-08",
				dimensions={"department": "FIN"},
				sequence_value=37,
				sequence_digits=6,
			),
			"PO-260808-FIN-000037",
		)

	def test_render_number_rejects_exhausted_sequence(self) -> None:
		with self.assertRaises(NumberingContractError):
			render_number(
				template="PO-{SEQ}",
				business_date="2026-08-08",
				dimensions={},
				sequence_value=100,
				sequence_digits=2,
			)

	def test_scheme_builder_normalizes_public_contract(self) -> None:
		command = build_numbering_scheme_upsert(
			code="po_main",
			display_name="采购订单编号",
			company="I-ONE",
			hospital="h001",
			template="PO-{YYYY}-{DIM:site}-{SEQ}",
			reset_policy="Yearly",
			sequence_digits="6",
			start_number="10",
			enabled="1",
			valid_from="2026-01-01",
			expected_revision="0",
		)
		self.assertEqual(command.code, "PO_MAIN")
		self.assertEqual(command.hospital, "H001")
		self.assertEqual(command.dimension_keys, ("site",))
		self.assertEqual(command.start_number, 10)
		self.assertTrue(command.enabled)
		self.assertEqual(len(command.template_digest), 64)
		self.assertEqual(command.as_request_payload()["expected_revision"], 0)

	def test_scheme_builder_rejects_invalid_dates_and_sequence_settings(self) -> None:
		base = {
			"code": "PO",
			"display_name": "采购订单编号",
			"company": "I-ONE",
			"hospital": "H001",
			"template": "PO-{SEQ}",
			"reset_policy": "Never",
			"sequence_digits": 2,
		}
		for changes in (
			{"start_number": 100},
			{"sequence_digits": 1},
			{"reset_policy": "Weekly"},
			{"valid_from": "2026-02-01", "valid_to": "2026-01-01"},
		):
			with self.subTest(changes=changes), self.assertRaises(NumberingContractError):
				build_numbering_scheme_upsert(**{**base, **changes})

	def test_allocation_builder_normalizes_date_scheme_and_dimensions(self) -> None:
		command = build_number_allocation(
			scheme="po",
			dimensions=json.dumps({"site": "xian"}),
			business_date="2026-08-08",
			required_dimension_keys=("site",),
		)
		self.assertEqual(command.scheme, "PO")
		self.assertEqual(command.dimensions, {"site": "XIAN"})
		self.assertEqual(command.business_date, "2026-08-08")

	def test_reservation_token_and_name_are_safe(self) -> None:
		test_token = "TEST-" + ("0" * 16)
		token = normalize_reservation_token(test_token)
		self.assertEqual(token, test_token)
		self.assertRegex(reservation_name_for(token), r"^num-[0-9a-f]{64}$")
		with self.assertRaises(NumberingContractError):
			normalize_reservation_token("short")


if __name__ == "__main__":
	unittest.main()
