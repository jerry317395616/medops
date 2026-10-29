from __future__ import annotations

import unittest

from medops.common.confirmation_privacy import ConfirmationPayload
from medops.common.reservation_commitment_request import (
	build_commitment_orders_query,
	build_commitment_post,
	build_commitment_prepare,
)


class CommitmentRequestTest(unittest.TestCase):
	def test_order_lookup_requires_complete_opaque_root_and_current_source_revision(self):
		self.assertEqual(
			build_commitment_orders_query({"reservation": "a" * 64, "source_digest": "b" * 64}),
			("a" * 64, "b" * 64),
		)

	def test_order_lookup_rejects_unbounded_or_client_selected_source_scope(self):
		base = {"reservation": "a" * 64, "source_digest": "b" * 64}
		for value in (
			{},
			{"reservation": "a" * 64},
			base | {"source_digest": "invalid"},
			base | {"request": "OTHER-MR"},
			base | {"filters": {"company": "other"}},
			base | {"offset": 100},
			base | {"budget_key": {}},
		):
			with self.subTest(value=value), self.assertRaises(ValueError):
				build_commitment_orders_query(value)

	def test_prepare_has_no_client_snapshot_or_scope_authority_and_hides_reason(self):
		command = build_commitment_prepare(self.preview())
		self.assertEqual(command.amount, "0.4")
		self.assertNotIn(command.reason, repr(command))

	def preview(self):
		return {
			"reservation": "a" * 64,
			"purchase_order": "PO-1",
			"purchase_order_line": "line-1",
			"amount": "0.400",
			"reason": "Synthetic private commitment reason",
		}

	def test_prepare_rejects_bad_amounts_unknown_fields_and_missing_links(self):
		for change in (
			{"amount": 0.4},
			{"amount": "0"},
			{"amount": "-1"},
			{"amount": "NaN"},
			{"amount": "0.0000000001"},
			{"reservation": "other"},
			{"purchase_order": ""},
			{"purchase_order_line": None},
			{"reason": ""},
			{"budget_key": {}},
			{"snapshot": "x"},
		):
			with self.subTest(change=change), self.assertRaises(ValueError):
				build_commitment_prepare(self.preview() | change)

	def test_post_requires_exact_explicit_boolean_acknowledgement(self):
		base = {
			"confirmation": "a" * 64,
			"proof": "synthetic_commitment_proof_1234567890123456",
			"acknowledge": True,
		}
		for change in (
			{"acknowledge": False},
			{"acknowledge": 1},
			{"acknowledge": "true"},
			{"confirmation": "other"},
			{"amount": "1"},
			{"proof": "short"},
		):
			with self.subTest(change=change), self.assertRaises(ValueError):
				build_commitment_post(base | change)

	def test_post_carrier_and_command_never_print_raw_proof(self):
		proof = "synthetic_commitment_proof_1234567890123456"
		command = build_commitment_post(
			ConfirmationPayload({"confirmation": "a" * 64, "proof": proof, "acknowledge": True})
		)
		self.assertEqual(command.proof, proof)
		self.assertNotIn(proof, repr(command))
		self.assertNotIn(proof, repr(command.proof))
