from __future__ import annotations

import unittest

from medops.common.confirmation_privacy import ConfirmationPayload
from medops.common.reservation_renewal_request import build_renewal_command, build_renewed_reservation_post


class ReservationRenewalRequestTest(unittest.TestCase):
	def test_valid_private_command_hides_proof(self):
		payload = {
			"original_confirmation": "a" * 64,
			"original_decision": "b" * 64,
			"renewal_confirmation": "c" * 64,
			"proof": "synthetic_renewal_confirmation_1234567890",
			"acknowledge": True,
		}
		command = build_renewal_command(ConfirmationPayload(payload))
		self.assertEqual(command.original_decision, payload["original_decision"])
		self.assertEqual(command.renewal_confirmation, payload["renewal_confirmation"])
		self.assertNotIn(payload["proof"], repr(command))
		self.assertNotIn(payload["proof"], repr(command.proof))

	def test_malformed_or_implicitly_acknowledged_command_fails(self):
		valid = {
			"original_confirmation": "a" * 64,
			"original_decision": "b" * 64,
			"renewal_confirmation": "c" * 64,
			"proof": "synthetic_renewal_confirmation_1234567890",
			"acknowledge": True,
		}
		for payload in (
			valid | {"original_confirmation": "a" * 63},
			valid | {"original_decision": "B" * 64},
			valid | {"renewal_confirmation": "c" * 63},
			valid | {"acknowledge": 1},
			valid | {"proof": "short"},
			valid | {"other": True},
			{key: value for key, value in valid.items() if key != "acknowledge"},
		):
			with self.subTest(payload=repr(payload)), self.assertRaises(ValueError):
				build_renewal_command(payload)

	def test_renewed_post_requires_explicit_latest_decision_and_hides_proof(self):
		payload = {
			"original_confirmation": "a" * 64,
			"original_decision": "b" * 64,
			"renewal_confirmation": "c" * 64,
			"renewal_decision": "d" * 64,
			"proof": "synthetic_renewal_confirmation_1234567890",
			"acknowledge": True,
		}
		command = build_renewed_reservation_post(ConfirmationPayload(payload))
		self.assertEqual(command.renewal_decision, payload["renewal_decision"])
		self.assertNotIn(payload["proof"], repr(command))
		for changed in (
			payload | {"renewal_decision": "d" * 63},
			payload | {"acknowledge": 1},
			payload | {"unexpected": "extra"},
			{k: v for k, v in payload.items() if k != "renewal_decision"},
		):
			with self.subTest(changed=repr(changed)), self.assertRaises(ValueError):
				build_renewed_reservation_post(changed)


if __name__ == "__main__":
	unittest.main()
