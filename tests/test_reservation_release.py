from __future__ import annotations

import copy
import hashlib
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from medops.common.authorized_reservation_projection import verify_authorized_released_reservation
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS
from medops.common.budget_reservation_proof import verify_reservation_records
from medops.common.budget_reservation_storage import reservation_record_from_row, reservation_record_row
from medops.common.initial_reservation_posting import renewed_initial_reservation_documents
from medops.common.reservation_authorization import (
	verify_reservation_authorization_rows,
	verify_reservation_confirmation_row,
)
from medops.common.reservation_release import (
	parse_reservation_release_snapshot,
	reservation_release_confirmation,
	reservation_release_decision,
	reservation_release_intent,
	reservation_release_record,
	reservation_release_row,
	reservation_release_snapshot,
	verify_released_reservation_records,
	verify_reservation_release_proof,
	verify_reservation_release_row,
)
from medops.common.reservation_renewal import reservation_renewal_decision
from tests.test_authorized_reservation_projection import evidence
from tests.test_material_source_snapshot import facts
from tests.test_reservation_authorization import ACTOR, PROOF, SESSION
from tests.test_reservation_renewal import RENEWAL_PROOF, RENEWAL_SESSION, renewal_confirmation


def stamp(value):
	return value.isoformat(sep=" ", timespec="microseconds")


def release_evidence(original, records, amount: object = "4.00", minute=10):
	position = verify_reservation_records(records)
	item = reservation_release_intent(original, position, records[-1], amount=amount, reason="Unused portion")
	now = datetime(2026, 1, 1, 10, minute)
	confirmed = reservation_release_confirmation(
		item,
		actor=ACTOR,
		proof_hash=hashlib.sha256(PROOF.encode()).hexdigest(),
		session_digest=SESSION,
		recorded_at=stamp(now),
		expires_at=stamp(now + timedelta(minutes=5)),
	)
	decided = reservation_release_decision(
		item,
		confirmed,
		proof=PROOF,
		actor=ACTOR,
		session_digest=SESSION,
		recorded_at=stamp(now + timedelta(seconds=1)),
		acknowledge=True,
	)
	record = reservation_release_record(
		item,
		confirmed,
		decided,
		records[-1],
		occurred_at=stamp(now + timedelta(seconds=2)),
	)
	return item, confirmed, decided, record


def rows(item, first, second):
	return reservation_release_row(item, first), reservation_release_row(item, second, first)


class ReservationReleaseTest(unittest.TestCase):
	def setUp(self):
		self.original, self.root, event_rows, self.original_confirmation, self.original_decision = evidence()
		self.records = [reservation_record_from_row(row) for row in event_rows]

	def test_authorized_history_uses_frozen_root_and_rejects_missing_release_decision(self):
		item, first, second, record = release_evidence(self.original, self.records)
		chain = [reservation_record_row(value) for value in [*self.records, record]]
		position, _, _ = verify_authorized_released_reservation(
			self.root,
			chain,
			self.original_confirmation,
			self.original_decision,
			[rows(item, first, second)],
			current_facts=facts(),
		)
		self.assertEqual((self.root["event_count"], self.root["reserved_amount"]), (1, "10.00"))
		self.assertEqual((position.last_sequence, position.open_reservation), (2, "6.00"))
		for changed_root, releases in (
			(self.root, []),
			(self.root | {"event_count": 2}, [rows(item, first, second)]),
			(self.root | {"reserved_amount": "6.00"}, [rows(item, first, second)]),
		):
			with self.assertRaises(ValueError):
				verify_authorized_released_reservation(
					changed_root,
					chain,
					self.original_confirmation,
					self.original_decision,
					releases,
					current_facts=facts(),
				)

	def test_release_preserves_original_authority_and_rejects_changed_financial_source(self):
		item, first, second, record = release_evidence(self.original, self.records)
		chain = [reservation_record_row(value) for value in [*self.records, record]]
		for original_decision, source, renewals in (
			(self.original_decision | {"actor": "other@example.com"}, facts(), []),
			(self.original_decision, facts(amount="31.000000000"), []),
			(self.original_decision, facts(), [(None, None)]),
		):
			with self.assertRaises(ValueError):
				verify_authorized_released_reservation(
					self.root,
					chain,
					self.original_confirmation,
					original_decision,
					[rows(item, first, second)],
					current_facts=source,
					renewals=renewals,
				)

	def test_valid_independent_decision_cannot_fork_from_stale_tail(self):
		item, first, second, record = release_evidence(self.original, self.records)
		other_item, other_first, other_second, other_record = release_evidence(
			self.original, self.records, "3.00", 20
		)
		with self.assertRaises(ValueError):
			verify_released_reservation_records(
				self.original,
				[*self.records, record, other_record],
				[rows(item, first, second), rows(other_item, other_first, other_second)],
			)

	def test_renewed_initial_root_remains_bound_to_full_original_lineage_after_release(self):
		_, first = verify_reservation_confirmation_row(self.original_confirmation)
		_, original = verify_reservation_authorization_rows(
			self.original_confirmation, self.original_decision
		)
		confirmed = renewal_confirmation(self.original)
		decision = reservation_renewal_decision(
			self.original,
			first,
			original,
			[],
			confirmed,
			proof=RENEWAL_PROOF,
			actor=confirmed.actor,
			session_digest=RENEWAL_SESSION,
			recorded_at="2026-01-01 10:05:01.000000",
			acknowledge=True,
		)
		lineage = [(confirmed, decision)]
		documents = renewed_initial_reservation_documents(
			self.original_confirmation,
			self.original_decision,
			lineage,
			occurred_at="2026-01-01 10:06:00.000000",
		)
		root = {name: documents.root[name] for name in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}}
		records = [reservation_record_from_row(dict(documents.event))]
		item, released_confirmation, released_decision, record = release_evidence(self.original, records)
		chain = [reservation_record_row(value) for value in [*records, record]]
		position, _, checked = verify_authorized_released_reservation(
			root,
			chain,
			self.original_confirmation,
			self.original_decision,
			[rows(item, released_confirmation, released_decision)],
			current_facts=facts(),
			renewals=lineage,
		)
		self.assertEqual((position.open_reservation, checked), ("6.00", decision))
		with self.assertRaises(ValueError):
			verify_authorized_released_reservation(
				root,
				chain,
				self.original_confirmation,
				self.original_decision,
				[rows(item, released_confirmation, released_decision)],
				current_facts=facts(),
				renewals=[],
			)

	def test_partial_then_full_release_preserves_original_and_reconstructs_exact_position(self):
		root_before = copy.deepcopy(self.root)
		item, first, second, record = release_evidence(self.original, self.records)
		pairs = [rows(item, first, second)]
		position = verify_released_reservation_records(self.original, [*self.records, record], pairs)
		self.assertEqual(
			(position.reserved, position.released, position.open_reservation), ("10.00", "4.00", "6.00")
		)
		next_item, confirmed, decided, next_record = release_evidence(
			self.original, [*self.records, record], "6.00", 20
		)
		pairs.append(rows(next_item, confirmed, decided))
		position = verify_released_reservation_records(
			self.original, [*self.records, record, next_record], pairs
		)
		self.assertEqual(
			(position.reserved, position.released, position.open_reservation), ("10.00", "10.00", "0.00")
		)
		self.assertEqual(self.root, root_before)
		self.assertNotEqual(second.one_time_key, decided.one_time_key)

	def test_amount_must_be_positive_exact_and_within_current_open_position(self):
		for amount in ("0.00", "-1.00", "10.01", "4", "4.001", 4, True):
			with self.subTest(amount=amount), self.assertRaises(ValueError):
				release_evidence(self.original, self.records, amount)
		_, _, _, record = release_evidence(self.original, self.records)
		with self.assertRaises(ValueError):
			release_evidence(self.original, [*self.records, record], "6.01")

	def test_proof_rejects_other_actor_session_changed_or_expired_token(self):
		item, first, _, _ = release_evidence(self.original, self.records)
		values = dict(proof=PROOF, actor=ACTOR, session_digest=SESSION, now="2026-01-01 10:10:01.000000")
		verify_reservation_release_proof(item, first, **values)
		for changed in (
			{"actor": "other@example.com"},
			{"session_digest": "e" * 64},
			{"proof": "x" * 43},
			{"now": "2026-01-01 10:15:00.000000"},
			{"now": "2026-01-01 10:09:59.000000"},
		):
			with self.subTest(changed=changed), self.assertRaises(ValueError):
				verify_reservation_release_proof(item, first, **(values | changed))
		with self.assertRaises(ValueError):
			reservation_release_decision(
				item,
				first,
				proof=PROOF,
				actor=ACTOR,
				session_digest=SESSION,
				recorded_at=values["now"],
				acknowledge=1,
			)

	def test_snapshot_and_database_rows_roundtrip_but_reject_tampering(self):
		item, first, second, _ = release_evidence(self.original, self.records)
		self.assertEqual(parse_reservation_release_snapshot(reservation_release_snapshot(item)), item)
		for row, confirmation in (
			(rows(item, first, second)[0], None),
			(rows(item, first, second)[1], first),
		):
			stored = row | {key: datetime.fromisoformat(row[key]) for key in ("recorded_at", "expires_at")}
			checked, _ = verify_reservation_release_row(stored, confirmation)
			self.assertEqual(checked, item)
			for changes in (
				{"previous_sequence": 1.0},
				{"previous_event_digest": "f" * 64},
				{"actor": "other@example.com"},
				{"proof_version": True},
				{"acknowledged": bool(row["acknowledged"])},
				{"extra": "unsupported"},
			):
				with self.subTest(changes=changes), self.assertRaises(ValueError):
					verify_reservation_release_row(stored | changes, confirmation)
		with self.assertRaises(ValueError):
			parse_reservation_release_snapshot(reservation_release_snapshot(item).replace('"4.00"', '"5.00"'))

	def test_tail_and_independent_evidence_cannot_be_reused_or_omitted(self):
		item, first, second, record = release_evidence(self.original, self.records)
		pair = rows(item, first, second)
		for records, pairs in (
			([*self.records, record], []),
			(self.records, [pair]),
			([*self.records, record, record], [pair, pair]),
		):
			with self.assertRaises(ValueError):
				verify_released_reservation_records(self.original, records, pairs)
		for changes in ({"previous_event_digest": "f" * 64}, {"previous_sequence": 2}, {"amount": "5.00"}):
			with self.assertRaises(ValueError):
				reservation_release_record(
					replace(item, **changes),
					first,
					second,
					self.records[-1],
					occurred_at=record.occurred_at,
				)

	def test_event_binds_decision_source_predecessor_and_valid_time(self):
		item, first, second, record = release_evidence(self.original, self.records)
		pair = rows(item, first, second)
		for changed in (
			replace(record, actor="other@example.com"),
			replace(record, source=replace(record.source, action="Release Reservation:unrelated")),
			replace(record, previous_digest="b" * 64),
		):
			with self.assertRaises(ValueError):
				verify_released_reservation_records(self.original, [*self.records, changed], [pair])
		for occurred_at in ("2026-01-01 10:10:00.000000", "2026-01-01 10:15:00.000000"):
			with self.assertRaises(ValueError):
				reservation_release_record(item, first, second, self.records[-1], occurred_at=occurred_at)

	def test_confirmation_cannot_predate_history_or_exceed_five_minutes(self):
		item, first, _, _ = release_evidence(self.original, self.records)
		for recorded_at, expires_at in (
			("2026-01-01 10:01:59.000000", "2026-01-01 10:05:00.000000"),
			(first.recorded_at, "2026-01-01 10:15:01.000000"),
		):
			with self.assertRaises(ValueError):
				reservation_release_confirmation(
					item,
					actor=ACTOR,
					proof_hash=first.proof_hash,
					session_digest=SESSION,
					recorded_at=recorded_at,
					expires_at=expires_at,
				)
