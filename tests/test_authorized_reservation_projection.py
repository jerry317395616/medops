from __future__ import annotations

import unittest

from medops.common.authorized_reservation_projection import verify_authorized_initial_reservation
from medops.common.budget_reservation_anchor import reservation_anchor
from medops.common.budget_reservation_events import ReservationEvent
from medops.common.budget_reservation_proof import (
	ReservationSource,
	reservation_event_key,
	reservation_record,
)
from medops.common.budget_reservation_storage import reservation_record_row
from medops.common.material_source_snapshot import build_snapshot_reservation_projection
from medops.common.reservation_authorization import reservation_authorization_row, reservation_decision
from tests.test_material_source_snapshot import facts
from tests.test_reservation_authorization import ACTOR, DECIDED, PROOF, SESSION, confirmation, intent


def evidence(
	*, amount="10.00", actor=ACTOR, occurred="2026-01-01 10:02:00.000000", allocation="allocation-a"
):
	item = intent(allocation=allocation)
	confirmed = confirmation(item)
	decision = reservation_decision(
		item,
		confirmed,
		proof=PROOF,
		actor=ACTOR,
		session_digest=SESSION,
		recorded_at=DECIDED,
		acknowledge=True,
	)
	source = ReservationSource(
		"Material Request", "MR-001", item.source_snapshot_digest, "MR-LINE-001", allocation, "Reserve"
	)
	name = "RESERVATION-001"
	event = ReservationEvent(
		reservation_event_key(name, 1), name, 1, item.key, item.currency, item.precision, "Reserve", amount
	)
	record = reservation_record(event, source, actor=actor, occurred_at=occurred, previous_digest=None)
	anchor = reservation_anchor(
		[record],
		expected_source=source,
		source_snapshot_digest=item.source_snapshot_digest,
		cost_center="Center",
	)
	root = build_snapshot_reservation_projection(
		anchor, [record], source_facts=facts(), allocation=allocation, expected_key=item.key
	)
	return (
		item,
		root,
		[reservation_record_row(record)],
		reservation_authorization_row(item, confirmed),
		reservation_authorization_row(item, confirmed, decision),
	)


class AuthorizedReservationProjectionTest(unittest.TestCase):
	def test_independent_decision_binds_single_initial_event_and_progressed_source(self):
		item, root, events, first, second = evidence()
		position, comparison, decision = verify_authorized_initial_reservation(
			root,
			events,
			first,
			second,
			current_facts=facts(status="Partially Ordered", request_modified="2026-01-02 09:00:00"),
		)
		self.assertEqual((position.reserved, position.open_reservation), (item.amount, item.amount))
		self.assertTrue(comparison.status_changed)
		self.assertEqual(decision.intent_digest, item.intent_digest)

	def test_valid_but_different_event_actor_amount_or_time_is_rejected(self):
		for changes in (
			{"amount": "9.00"},
			{"actor": "other@example.com"},
			{"occurred": "2026-01-01 10:00:00.000000"},
		):
			_, root, events, first, second = evidence(**changes)
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				verify_authorized_initial_reservation(root, events, first, second, current_facts=facts())

	def test_other_valid_decision_cannot_authorize_this_allocation(self):
		_, root, events, _, _ = evidence()
		_, _, _, first, second = evidence(allocation="allocation-b")
		with self.assertRaises(ValueError):
			verify_authorized_initial_reservation(root, events, first, second, current_facts=facts())

	def test_changed_source_or_tampered_decision_is_rejected(self):
		_, root, events, first, second = evidence()
		with self.assertRaises(ValueError):
			verify_authorized_initial_reservation(
				root, events, first, second, current_facts=facts(amount="31.000000000")
			)
		with self.assertRaises(ValueError):
			verify_authorized_initial_reservation(
				root, events, first, second | {"actor": "other@example.com"}, current_facts=facts()
			)

	def test_missing_extra_or_tampered_event_fails_closed(self):
		_, root, events, first, second = evidence()
		for rows in ([], events + events, [events[0] | {"amount": "11.00"}]):
			with self.assertRaises(ValueError):
				verify_authorized_initial_reservation(root, rows, first, second, current_facts=facts())
