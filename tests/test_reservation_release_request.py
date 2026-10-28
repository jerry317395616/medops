from __future__ import annotations

import unittest
from dataclasses import replace

from medops.common.confirmation_privacy import ConfirmationPayload
from medops.common.reservation_release_request import (
	build_release_reservation_post,
	build_release_reservation_prepare,
)


class ReservationReleaseRequestTest(unittest.TestCase):
	def test_prepare_normalizes_amount_and_keeps_reason_private(self):
		command = build_release_reservation_prepare(
			{"reservation": "a" * 64, "amount": "0.400", "reason": "Unused amount"}
		)
		self.assertEqual(command.amount, "0.4")
		self.assertNotIn(command.reason, repr(command))
		self.assertNotEqual(command, replace(command, amount="0.5"))

	def test_prepare_rejects_invalid_or_extra_financial_input(self):
		base = {"reservation": "a" * 64, "amount": "1", "reason": "Unused"}
		for change in (
			{"amount": 0},
			{"amount": 0.4},
			{"amount": "-1"},
			{"amount": "NaN"},
			{"amount": "0.0000000001"},
			{"reservation": "untrusted"},
			{"reason": ""},
			{"acknowledge": True},
		):
			with self.subTest(change=change), self.assertRaises(ValueError):
				build_release_reservation_prepare(base | change)

	def test_post_requires_explicit_acknowledgement_and_exact_fields(self):
		base = {
			"confirmation": "a" * 64,
			"proof": "synthetic_release_proof_12345678901234567890",
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
				build_release_reservation_post(base | change)

	def test_post_preserves_confirmation_carrier_without_printing_proof(self):
		base = {
			"confirmation": "a" * 64,
			"proof": "synthetic_release_proof_12345678901234567890",
			"acknowledge": True,
		}
		command = build_release_reservation_post(ConfirmationPayload(base))
		self.assertEqual(command.proof, base["proof"])
		self.assertNotIn(base["proof"], repr(command))
		self.assertNotIn(base["proof"], repr(command.proof))
