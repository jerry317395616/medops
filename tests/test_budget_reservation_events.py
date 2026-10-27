from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import localcontext
from typing import Any

from medops.common.budget_control import available_budget, budget_control_key
from medops.common.budget_reservation_events import ReservationEvent, replay_reservation_events
from tests.test_budget_control import key


def event(sequence_value, kind_value, amount_value, **changes):
	values: dict[str, Any] = dict(
		name=f"event-{sequence_value}",
		reservation="reservation-1",
		sequence=sequence_value,
		key=budget_control_key(key()),
		currency="CNY",
		precision=2,
		kind=kind_value,
		amount=amount_value,
	)
	return ReservationEvent(**(values | changes))


class ReservationEventReplayTest(unittest.TestCase):
	def test_partial_release_and_transfers_do_not_double_count(self):
		position = replay_reservation_events(
			[
				event(1, "Reserve", "100.00"),
				event(2, "Release Reservation", "10.00"),
				event(3, "Commit", "60.00"),
				event(4, "Actual", "20.00"),
				event(5, "Release Commitment", "15.00"),
			]
		)
		self.assertEqual(
			(position.reserved, position.committed, position.actual, position.released),
			("100.00", "60.00", "20.00", "105.00"),
		)
		self.assertEqual((position.open_reservation, position.open_commitment), ("30.00", "25.00"))
		components = dict(
			approved="1000.00",
			adjustment_in="0",
			adjustment_out="0",
			reserved=position.reserved,
			committed=position.committed,
			actual=position.actual,
			released=position.released,
		)
		self.assertEqual(available_budget(components, 2), "925.00")

	def test_reserve_can_be_incremented_after_partial_use(self):
		position = replay_reservation_events(
			[event(1, "Reserve", "5.00"), event(2, "Commit", "5.00"), event(3, "Reserve", "2.00")]
		)
		self.assertEqual((position.open_reservation, position.open_commitment), ("2.00", "5.00"))

	def test_release_and_conversion_cannot_exceed_open_state(self):
		for second in ("Release Reservation", "Commit", "Actual", "Release Commitment"):
			with self.subTest(kind=second), self.assertRaises(ValueError):
				replay_reservation_events([event(1, "Reserve", "1.00"), event(2, second, "2.00")])
		with self.assertRaises(ValueError):
			replay_reservation_events([event(1, "Commit", "1.00")])

	def test_missing_out_of_order_or_duplicate_events_are_rejected(self):
		first = event(1, "Reserve", "5.00")
		for chain in (
			[],
			[first, event(3, "Commit", "1.00")],
			[first, event(2, "Commit", "1.00", name=first.name)],
		):
			with self.subTest(chain=chain), self.assertRaises(ValueError):
				replay_reservation_events(chain)

	def test_reservation_key_currency_and_precision_cannot_change(self):
		first = event(1, "Reserve", "5.00")
		for changes in (
			{"reservation": "other"},
			{"key": budget_control_key(key(organization_unit="Other"))},
			{"currency": "USD"},
			{"precision": 3, "amount": "1.000"},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				replay_reservation_events([first, event(2, "Commit", "1.00", **changes)])

	def test_invalid_noncanonical_and_unsupported_events_are_rejected(self):
		for changes in (
			{"amount": "0.00"},
			{"amount": "1"},
			{"amount": 1.0},
			{"precision": None},
			{"sequence": True},
			{"kind": "Refund"},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				event(1, "Reserve", "1.00", **changes)

	def test_high_precision_is_independent_of_decimal_context(self):
		first = event(1, "Reserve", "999999999999.123456789", precision=9)
		second = event(2, "Commit", "0.000000001", precision=9)
		with localcontext() as context:
			context.prec = 6
			position = replay_reservation_events([first, second])
		self.assertEqual(position.open_reservation, "999999999999.123456788")
		self.assertEqual(position.open_commitment, "0.000000001")

	def test_frozen_event_and_cross_reservation_chain(self):
		first = event(1, "Reserve", "1.00")
		with self.assertRaises(ValueError):
			replay_reservation_events([first, replace(event(2, "Commit", "1.00"), reservation="different")])


if __name__ == "__main__":
	unittest.main()
