from __future__ import annotations

import json
import unittest
from copy import deepcopy
from hashlib import sha256

from medops.common.budget_workflow import HumanConfirmation, build_human_confirmation
from medops.common.confirmation_privacy import REDACTED_CONFIRMATION, ConfirmationPayload


class TestConfirmationPrivacy(unittest.TestCase):
	def test_rpc_carrier_redacts_repr_json_and_diagnostic_deepcopy(self):
		proof = "syntheticProof_" + "a" * 32
		for value in (
			{"confirmation": "Review", "proof": proof, "acknowledge": True},
			json.dumps({"confirmation": "Review", "proof": proof, "acknowledge": True}),
		):
			with self.subTest(transport=type(value).__name__):
				body = {"payload": ConfirmationPayload(value)}
				self.assertNotIn(proof, repr(body))
				self.assertNotIn(proof, json.dumps(body))
				self.assertEqual(deepcopy(body), {"payload": REDACTED_CONFIRMATION})
				command = build_human_confirmation(body["payload"])
				self.assertEqual(command.proof, proof)
				self.assertEqual(sha256(command.proof.encode()).digest(), sha256(proof.encode()).digest())
				self.assertNotIn(proof, repr(command))
				self.assertNotIn(proof, repr({"proof": command.proof}))

	def test_redaction_does_not_accept_a_mask_or_relax_confirmation_validation(self):
		for value in (
			REDACTED_CONFIRMATION,
			{"confirmation": "Review", "proof": "a" * 43, "acknowledge": False},
			{"confirmation": "Review", "proof": "bad", "acknowledge": True},
			{"confirmation": "Review", "proof": "a" * 43, "acknowledge": True, "amount": "1"},
		):
			with self.subTest(kind=type(value).__name__), self.assertRaises(ValueError):
				build_human_confirmation(ConfirmationPayload(value))

	def test_direct_service_command_also_hides_proof_in_traceback_values(self):
		command = HumanConfirmation("Review", "b" * 43, True)
		self.assertNotIn("b" * 43, repr(command))
		self.assertNotIn("b" * 43, repr(command.proof))
		self.assertEqual(command.proof.encode(), b"b" * 43)
