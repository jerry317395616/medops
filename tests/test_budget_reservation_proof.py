from __future__ import annotations

import unittest
from dataclasses import replace

from medops.common.budget_control import budget_control_key
from medops.common.budget_reservation_events import ReservationEvent
from medops.common.budget_reservation_proof import (
	ReservationSource,
	reservation_event_key,
	reservation_record,
	reservation_source_action_key,
	verify_reservation_records,
)
from tests.test_budget_control import key


def source(**changes):
	values = dict(
		doctype="Purchase Request", name="PR-1", revision="2", line="LINE-1", allocation="A", action="approve"
	)
	return ReservationSource(**(values | changes))


def record(sequence, kind, amount, *, previous=None, reservation="reservation-1", origin=None):
	event = ReservationEvent(
		reservation_event_key(reservation, sequence),
		reservation,
		sequence,
		budget_control_key(key()),
		"CNY",
		2,
		kind,
		amount,
	)
	return reservation_record(
		event,
		origin or source(action=f"action-{sequence}"),
		actor="manager@example.com",
		occurred_at="2026-10-27 09:30:00.000000",
		previous_digest=previous,
	)


class ReservationProofTest(unittest.TestCase):
	def test_linked_chain_replays_and_has_deterministic_keys(self):
		first = record(1, "Reserve", "10.00")
		second = record(2, "Commit", "6.00", previous=first.event_digest)
		position = verify_reservation_records([first, second])
		self.assertEqual((position.open_reservation, position.open_commitment), ("4.00", "6.00"))
		self.assertEqual(first, record(1, "Reserve", "10.00"))
		self.assertNotEqual(first.event.name, first.sequence_key)

	def test_source_action_identity_does_not_depend_on_target_reservation(self):
		origin = source()
		one = record(1, "Reserve", "10.00", reservation="one", origin=origin)
		two = record(1, "Reserve", "10.00", reservation="two", origin=origin)
		self.assertNotEqual(one.event.name, two.event.name)
		self.assertEqual(one.source_action_key, two.source_action_key)
		self.assertNotEqual(one.source_action_key, reservation_source_action_key(source(allocation="B")))

	def test_changes_to_evidence_are_detected(self):
		first = record(1, "Reserve", "10.00")
		for forged in (
			replace(first, actor="other@example.com"),
			replace(first, occurred_at="2026-10-27 09:30:01.000000"),
			replace(first, source=source(action="different")),
			replace(first, event=replace(first.event, amount="9.00")),
			replace(first, sequence_key="0" * 64),
			replace(first, source_action_key="0" * 64),
			replace(first, event_digest="0" * 64),
		):
			with self.subTest(field=forged), self.assertRaises(ValueError):
				verify_reservation_records([forged])

	def test_missing_reordered_or_duplicate_source_actions_are_rejected(self):
		first = record(1, "Reserve", "10.00")
		second = record(2, "Commit", "2.00", previous=first.event_digest, origin=first.source)
		for chain in ([second], [second, first], [first, second]):
			with self.subTest(chain=chain), self.assertRaises(ValueError):
				verify_reservation_records(chain)

	def test_predecessor_and_time_must_be_canonical(self):
		first = record(1, "Reserve", "10.00")
		second = record(2, "Commit", "2.00", previous=first.event_digest)
		for stamp in ("2026-10-27T09:30:00", "2026-10-27 09:30:00.000000+00:00", "invalid"):
			with self.subTest(stamp=stamp), self.assertRaises(ValueError):
				reservation_record(
					first.event, first.source, actor=first.actor, occurred_at=stamp, previous_digest=None
				)
		with self.assertRaises(ValueError):
			reservation_record(
				second.event,
				second.source,
				actor=second.actor,
				occurred_at=second.occurred_at,
				previous_digest=None,
			)
		with self.assertRaises(ValueError):
			verify_reservation_records([first, replace(second, previous_digest="0" * 64)])

	def test_bad_event_and_source_identities_are_rejected(self):
		first = record(1, "Reserve", "10.00")
		with self.assertRaises(ValueError):
			reservation_record(
				replace(first.event, name="other"),
				first.source,
				actor=first.actor,
				occurred_at=first.occurred_at,
				previous_digest=None,
			)
		for changes in ({"line": ""}, {"allocation": " A "}, {"revision": "2\n3"}):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				source(**changes)
		for sequence in (True, "1", 0, 10001):
			with self.subTest(sequence=sequence), self.assertRaises(ValueError):
				reservation_event_key("reservation", sequence)


if __name__ == "__main__":
	unittest.main()
