from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime

from medops.common.authorized_reservation_projection import (
	verify_authorized_initial_reservation,
	verify_renewed_initial_reservation,
)
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS
from medops.common.budget_reservation_proof import reservation_record
from medops.common.budget_reservation_storage import reservation_record_from_row
from medops.common.initial_reservation_posting import renewed_initial_reservation_documents
from medops.common.reservation_authorization import (
	verify_reservation_authorization_rows,
	verify_reservation_confirmation_row,
)
from medops.common.reservation_renewal import reservation_renewal_decision
from tests.test_authorized_reservation_projection import evidence
from tests.test_budget_reservation_proof import record
from tests.test_material_source_snapshot import facts
from tests.test_reservation_authorization import ACTOR
from tests.test_reservation_renewal import RENEWAL_PROOF, RENEWAL_SESSION, renewal_confirmation


class RenewedReservationPostingTest(unittest.TestCase):
	def setUp(self):
		self.item, _, _, self.first, self.original_row = evidence()
		_, self.original_confirmation = verify_reservation_confirmation_row(self.first)
		_, self.original = verify_reservation_authorization_rows(self.first, self.original_row)
		self.confirmed = renewal_confirmation(self.item, actor="renewing-manager@example.com")
		self.renewal = reservation_renewal_decision(
			self.item,
			self.original_confirmation,
			self.original,
			[],
			self.confirmed,
			proof=RENEWAL_PROOF,
			actor="renewing-manager@example.com",
			session_digest=RENEWAL_SESSION,
			recorded_at="2026-01-01 10:05:01.000000",
			acknowledge=True,
		)
		self.chain = [(self.confirmed, self.renewal)]
		self.documents = renewed_initial_reservation_documents(
			self.first, self.original_row, self.chain, occurred_at="2026-01-01 10:06:00.000000"
		)
		self.root = {
			field: self.documents.root[field] for field in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}
		}
		self.event = dict(self.documents.event)

	def verify(self, *, root=None, event=None, chain=None):
		return verify_renewed_initial_reservation(
			self.root if root is None else root,
			[self.event if event is None else event],
			self.first,
			self.original_row,
			self.chain if chain is None else chain,
			current_facts=facts(),
		)

	def test_v1_digest_remains_identical_to_pre_upgrade(self):
		self.assertEqual(
			record(1, "Reserve", "10.00").event_digest,
			"ae1606c2f887de407d36b5c97b8e13e0aa0c4417655a289a0696b7fc183adadf",
		)

	def test_renewed_event_binds_final_decision_and_new_actor(self):
		position, _, decision = self.verify()
		self.assertEqual(position.open_reservation, self.item.amount)
		self.assertEqual(decision, self.renewal)
		self.assertEqual(self.event["proof_version"], 2)
		self.assertEqual(
			(self.event["renewal_decision"], self.event["renewal_digest"]),
			(self.renewal.name, self.renewal.event_digest),
		)
		self.assertEqual(self.documents.root["created_by"], "renewing-manager@example.com")
		self.assertNotEqual(self.documents.root["created_by"], ACTOR)
		native = self.event | {"occurred_at": datetime.fromisoformat(self.event["occurred_at"])}
		self.assertEqual(reservation_record_from_row(native), reservation_record_from_row(self.event))

	def test_v1_verifier_never_accepts_renewed_event_without_lineage(self):
		with self.assertRaises(ValueError):
			verify_authorized_initial_reservation(
				self.root, [self.event], self.first, self.original_row, current_facts=facts()
			)

	def test_modified_or_incomplete_event_evidence_is_rejected(self):
		for changes in (
			{"renewal_decision": "f" * 64},
			{"renewal_digest": "f" * 64},
			{"renewal_digest": None},
			{"proof_version": 1},
			{"proof_version": True},
			{"actor": "other@example.com"},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				self.verify(event=self.event | changes)
		for chain in ([], [(self.confirmed, replace(self.renewal, event_digest="f" * 64))]):
			with self.assertRaises(ValueError):
				self.verify(chain=chain)

	def test_event_time_must_be_within_renewal_window(self):
		for stamp in ("2026-01-01 10:05:00.000000", "2026-01-01 10:10:00.000000"):
			with self.subTest(stamp=stamp), self.assertRaises(ValueError):
				renewed_initial_reservation_documents(
					self.first, self.original_row, self.chain, occurred_at=stamp
				)

	def test_old_renewal_cannot_authorize_event_if_a_successor_is_supplied(self):
		next_confirmation = renewal_confirmation(self.item, start=self.renewal.expires_at)
		next_decision = reservation_renewal_decision(
			self.item,
			self.original_confirmation,
			self.original,
			self.chain,
			next_confirmation,
			proof=RENEWAL_PROOF,
			actor=ACTOR,
			session_digest=RENEWAL_SESSION,
			recorded_at="2026-01-01 10:10:01.000000",
			acknowledge=True,
		)
		with self.assertRaises(ValueError):
			self.verify(chain=[*self.chain, (next_confirmation, next_decision)])

	def test_renewal_reference_is_not_allowed_on_later_event(self):
		first = record(1, "Reserve", "10.00")
		second = record(2, "Commit", "1.00", previous=first.event_digest)
		with self.assertRaises(ValueError):
			reservation_record(
				second.event,
				second.source,
				actor=second.actor,
				occurred_at=second.occurred_at,
				previous_digest=second.previous_digest,
				renewal_decision=self.renewal.name,
				renewal_digest=self.renewal.event_digest,
			)


if __name__ == "__main__":
	unittest.main()
