from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from medops.common.reservation_authorization import reservation_confirmation, reservation_decision
from medops.common.reservation_renewal import (
	MAX_RENEWALS,
	reservation_renewal_decision,
	reservation_renewal_row,
	verify_reservation_renewal_chain,
	verify_reservation_renewal_row,
)
from tests.test_reservation_authorization import ACTOR, DECIDED, EXPIRES, PROOF, SESSION, confirmation, intent

RENEWAL_PROOF = "synthetic_renewal_confirmation_1234567890"
RENEWAL_SESSION = "b" * 64


def stamp(value):
	return value.isoformat(sep=" ", timespec="microseconds")


def renewal_confirmation(item, *, start=EXPIRES, proof=RENEWAL_PROOF, actor=ACTOR, session=RENEWAL_SESSION):
	when = datetime.fromisoformat(start)
	return reservation_confirmation(
		item,
		actor=actor,
		proof_hash=hashlib.sha256(proof.encode()).hexdigest(),
		session_digest=session,
		recorded_at=stamp(when),
		expires_at=stamp(when + timedelta(minutes=5)),
	)


class ReservationRenewalTest(unittest.TestCase):
	def setUp(self):
		self.item = intent()
		self.first = confirmation(self.item)
		self.original = reservation_decision(
			self.item,
			self.first,
			proof=PROOF,
			actor=ACTOR,
			session_digest=SESSION,
			recorded_at=DECIDED,
			acknowledge=True,
		)
		self.new = renewal_confirmation(self.item)

	def build(self, *, renewals=(), confirmed=None, **changes):
		args = (
			dict(
				proof=RENEWAL_PROOF,
				actor=ACTOR,
				session_digest=RENEWAL_SESSION,
				recorded_at="2026-01-01 10:05:01.000000",
				acknowledge=True,
			)
			| changes
		)
		return reservation_renewal_decision(
			self.item, self.first, self.original, renewals, confirmed or self.new, **args
		)

	def test_expired_decision_can_be_renewed_without_changing_original(self):
		decision = self.build()
		self.assertEqual(decision.previous_decision, self.original.name)
		self.assertEqual(decision.original_decision, self.original.name)
		self.assertEqual((decision.sequence, decision.confirmation), (1, self.new.name))
		self.assertEqual(
			verify_reservation_renewal_chain(self.item, self.first, self.original, ()), self.original
		)
		self.assertEqual(
			verify_reservation_renewal_chain(self.item, self.first, self.original, [(self.new, decision)]),
			decision,
		)

	def test_renewal_binds_current_proof_actor_session_and_explicit_acknowledgement(self):
		for changes in (
			{"proof": "incorrect_renewal_confirmation_123456789"},
			{"actor": "other@example.com"},
			{"session_digest": "c" * 64},
			{"acknowledge": 1},
			{"recorded_at": self.new.expires_at},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				self.build(**changes)

	def test_renewal_rejects_unexpired_predecessor_and_changed_intent(self):
		early = renewal_confirmation(self.item, start="2026-01-01 10:04:59.000000")
		with self.assertRaises(ValueError):
			self.build(confirmed=early)
		changed = renewal_confirmation(intent(amount="11.00"))
		with self.assertRaises(ValueError):
			self.build(confirmed=changed)

	def test_successive_renewals_form_ordered_lineage(self):
		one = self.build()
		second = renewal_confirmation(self.item, start=one.expires_at)
		two = self.build(
			renewals=[(self.new, one)], confirmed=second, recorded_at="2026-01-01 10:10:01.000000"
		)
		self.assertEqual(
			(two.original_decision, two.previous_decision, two.sequence), (self.original.name, one.name, 2)
		)
		self.assertNotEqual(one.one_time_key, two.one_time_key)
		self.assertEqual(
			verify_reservation_renewal_chain(
				self.item, self.first, self.original, [(self.new, one), (second, two)]
			),
			two,
		)
		for broken in ([(second, two)], [(second, two), (self.new, one)]):
			with self.subTest(broken=broken), self.assertRaises(ValueError):
				verify_reservation_renewal_chain(self.item, self.first, self.original, broken)

	def test_competing_successors_share_unique_predecessor_key(self):
		one = self.build()
		other_proof = "other_synthetic_renewal_confirmation_123456"
		other = renewal_confirmation(self.item, proof=other_proof, actor="other@example.com")
		two = self.build(confirmed=other, proof=other_proof, actor="other@example.com")
		self.assertNotEqual(one.name, two.name)
		self.assertEqual(one.one_time_key, two.one_time_key)

	def test_tampered_evidence_and_wrong_original_are_rejected(self):
		one = self.build()
		for changed in (
			replace(one, sequence=2),
			replace(one, sequence=True),
			replace(one, acknowledged=1),
			replace(one, previous_decision="f" * 64),
			replace(one, acknowledged=False),
			replace(one, event_digest="f" * 64),
			replace(one, actor="other@example.com"),
		):
			with self.subTest(changed=changed), self.assertRaises(ValueError):
				verify_reservation_renewal_chain(self.item, self.first, self.original, [(self.new, changed)])
		with self.assertRaises(ValueError):
			verify_reservation_renewal_chain(self.item, self.first, replace(self.original, actor="other"), [])
		with self.assertRaises(ValueError):
			self.build(confirmed=replace(self.new, event_digest="f" * 64))

	def test_storage_row_roundtrip_reconstructs_exact_decision_and_database_times(self):
		decision = self.build()
		row = reservation_renewal_row(self.item, self.first, self.original, [], self.new, decision)
		self.assertEqual(row["evidence_key"], decision.name)
		row["recorded_at"] = datetime.fromisoformat(row["recorded_at"])
		row["expires_at"] = datetime.fromisoformat(row["expires_at"])
		self.assertEqual(
			verify_reservation_renewal_row(self.item, self.first, self.original, [], self.new, row), decision
		)

	def test_storage_row_rejects_forged_scope_chain_and_noncanonical_fields(self):
		decision = self.build()
		row = reservation_renewal_row(self.item, self.first, self.original, [], self.new, decision)
		for changes in (
			{"company": "other"},
			{"previous_decision": "f" * 64},
			{"proof_version": True},
			{"acknowledged": True},
			{"sequence": True},
			{"recorded_at": "2026-01-01T10:05:01"},
			{"evidence_key": "f" * 64},
			{"name": "f" * 64},
			{"actor": "other@example.com"},
			{"extra": 1},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				verify_reservation_renewal_row(
					self.item, self.first, self.original, [], self.new, row | changes
				)

	def test_rejects_malformed_or_oversized_chains(self):
		for chain in (None, [()], [None], [()] * (MAX_RENEWALS + 1)):
			with self.subTest(size=len(chain) if chain else 0), self.assertRaises(ValueError):
				verify_reservation_renewal_chain(self.item, self.first, self.original, chain)


if __name__ == "__main__":
	unittest.main()
