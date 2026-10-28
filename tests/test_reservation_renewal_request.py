from __future__ import annotations

import unittest

from medops.common.confirmation_privacy import ConfirmationPayload
from medops.common.reservation_renewal_request import build_renewal_command


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


if __name__ == "__main__":
	unittest.main()
