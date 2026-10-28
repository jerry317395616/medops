from __future__ import annotations

import unittest

from medops.common.confirmation_privacy import ConfirmationPayload
from medops.common.reservation_request import build_reservation_post


class ReservationPostRequestTest(unittest.TestCase):
	def test_valid_command_hides_proof_and_accepts_private_carrier(self):
		payload = {
			"confirmation": "a" * 64,
			"decision": "b" * 64,
			"proof": "synthetic_confirmation_proof_1234567890",
			"acknowledge": True,
		}
		command = build_reservation_post(ConfirmationPayload(payload))
		self.assertEqual(command.confirmation, payload["confirmation"])
		self.assertEqual(command.decision, payload["decision"])
		self.assertNotIn(payload["proof"], repr(command))
		self.assertNotIn(payload["proof"], repr(command.proof))

	def test_bad_request_is_rejected(self):
		valid = {
			"confirmation": "a" * 64,
			"decision": "b" * 64,
			"proof": "synthetic_confirmation_proof_1234567890",
			"acknowledge": True,
		}
		cases = [
			valid | {"acknowledge": 1},
			valid | {"decision": "b" * 63},
			valid | {"confirmation": "A" * 64},
			valid | {"proof": "short"},
			valid | {"unexpected": "value"},
			{k: v for k, v in valid.items() if k != "decision"},
		]
		for payload in cases:
			with self.subTest(payload=repr(payload)), self.assertRaises(ValueError):
				build_reservation_post(payload)


if __name__ == "__main__":
	unittest.main()
