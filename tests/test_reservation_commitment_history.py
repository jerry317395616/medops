from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from unittest.mock import patch

from medops.common.authorized_reservation_projection import verify_authorized_released_reservation
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS, verify_frozen_reservation_anchor_row
from medops.common.budget_reservation_proof import (
	reservation_event_key,
	reservation_record,
	verify_reservation_records,
)
from medops.common.budget_reservation_storage import reservation_record_from_row, reservation_record_row
from medops.common.commitment_evidence import verify_commitment_evidence_row
from medops.common.initial_reservation_posting import initial_reservation_documents
from medops.common.reservation_commitment_history import (
	COMMIT_ACTION_PREFIX,
	commitment_record,
	verify_reservation_action_records,
)
from medops.tests.commitment_evidence_fixtures import history_commitment
from medops.tests.reservation_release_fixtures import release_evidence
from medops.tests.reservation_release_fixtures import rows as release_rows
from tests.test_authorized_reservation_projection import evidence
from tests.test_material_source_snapshot import facts


def initial_history():
	original, _, initial_rows, first, second = evidence()
	documents = initial_reservation_documents(first, second, occurred_at=initial_rows[0]["occurred_at"])
	root = {name: documents.root[name] for name in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}}
	return original, root, [reservation_record_from_row(documents.event)], first, second


class ReservationCommitmentHistoryTest(unittest.TestCase):
	def setUp(self):
		self.original, self.root, self.records, self.first, self.second = initial_history()
		self.item, self.pair, self.record = history_commitment(self.original, self.records)

	def verify(self, records=None, *, releases=(), commitments=None, root=None, current=None):
		return verify_authorized_released_reservation(
			self.root if root is None else root,
			[reservation_record_row(r) for r in (records or [*self.records, self.record])],
			self.first,
			self.second,
			releases,
			commitments=[self.pair] if commitments is None else commitments,
			current_facts=facts() if current is None else current,
		)

	def test_commit_binds_native_order_evidence_and_retains_frozen_material_lineage(self):
		position, _, _ = self.verify()
		self.assertEqual(
			(position.open_reservation, position.open_commitment, position.committed),
			("6.00", "4.00", "4.00"),
		)
		self.assertEqual(self.record.source.action, COMMIT_ACTION_PREFIX + self.pair[1]["name"])
		self.assertEqual(replace(self.record.source, action="Reserve"), self.records[0].source)
		self.assertEqual((self.root["event_count"], self.root["reserved_amount"]), (1, "10.00"))
		self.assertEqual(self.pair[1]["purchase_order"], "PO-001")

	def test_missing_or_orphan_commitment_decision_cannot_extend_receipt(self):
		for records, commitments in ((None, []), (self.records, [self.pair]), (None, [self.pair, self.pair])):
			with self.subTest(commitments=len(commitments)), self.assertRaises(ValueError):
				self.verify(records, commitments=commitments)

	def test_structurally_valid_actor_amount_source_or_action_forgery_is_rejected(self):
		for event, source, actor in (
			(self.record.event, self.record.source, "other@example.com"),
			(replace(self.record.event, amount="3.00"), self.record.source, self.record.actor),
			(self.record.event, replace(self.record.source, action="Commit:" + "f" * 64), self.record.actor),
			(self.record.event, replace(self.record.source, line="other-line"), self.record.actor),
		):
			forged = reservation_record(
				event,
				source,
				actor=actor,
				occurred_at=self.record.occurred_at,
				previous_digest=self.record.previous_digest,
			)
			with self.assertRaises(ValueError):
				self.verify([*self.records, forged])

	def test_private_order_snapshot_and_columns_are_verified_before_event_replay(self):
		for changes in (
			{"purchase_order": "other-order"},
			{"order_snapshot_json": self.pair[0]["order_snapshot_json"] + " "},
			{"current_source_digest": "f" * 64},
			{"source_modified_at": "2025-01-01 00:00:00.000000"},
		):
			pair = (self.pair[0] | changes, self.pair[1])
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				self.verify(commitments=[pair])

	def test_release_commit_release_commit_mixed_history_is_fully_authorized(self):
		item, first, second, released = release_evidence(self.original, self.records, "2.00", 5)
		pairs = [release_rows(item, first, second)]
		records = [*self.records, released]
		_, committed_pair, committed = history_commitment(self.original, records, amount="3.00")
		records.append(committed)
		item, first, second, released = release_evidence(self.original, records, "1.00", 10)
		pairs.append(release_rows(item, first, second))
		records.append(released)
		_, last_pair, last = history_commitment(self.original, records, amount="4.00")
		records.append(last)
		position, _, _ = self.verify(records, releases=pairs, commitments=[last_pair, committed_pair])
		self.assertEqual(
			(position.open_reservation, position.open_commitment, position.released),
			("0.00", "7.00", "10.00"),
		)
		with self.assertRaises(ValueError):
			self.verify(records, releases=pairs[:1], commitments=[last_pair, committed_pair])

	def test_decision_cannot_be_substituted_between_predecessors(self):
		_, pair, record = history_commitment(self.original, [*self.records, self.record], amount="2.00")
		with self.assertRaises(ValueError):
			self.verify([*self.records, self.record, record], commitments=[pair, pair])
		with self.assertRaises(ValueError):
			self.verify(commitments=[pair])

	def test_current_material_progress_is_accepted_and_financial_change_rejected(self):
		self.verify(current=facts(status="Ordered", request_modified="2026-01-03 11:00:00"))
		for change in ({"amount": "31.000000000"}, {"cost_center": "other"}, {"quantity": "6.000000000"}):
			with self.assertRaises(ValueError):
				self.verify(current=facts(**change))

	def test_source_identity_cannot_replace_actual_persisted_root_identity(self):
		first = self.records[0]
		event = replace(
			first.event,
			reservation=self.original.reservation_identity,
			name=reservation_event_key(self.original.reservation_identity, 1),
		)
		forged = reservation_record(
			event, first.source, actor=first.actor, occurred_at=first.occurred_at, previous_digest=None
		)
		with self.assertRaises(ValueError):
			history_commitment(self.original, [forged])

	def test_event_window_canonical_time_and_predecessor_are_required(self):
		item, confirmed = verify_commitment_evidence_row(self.pair[0])
		_, decision = verify_commitment_evidence_row(self.pair[1], confirmed)
		for at, previous in (
			(decision.expires_at, self.records[0]),
			(decision.recorded_at[:-7], self.records[0]),
			(self.record.occurred_at, replace(self.records[0], event_digest="f" * 64)),
		):
			with self.assertRaises(ValueError):
				commitment_record(
					item,
					confirmed,
					decision,
					previous,
					material_snapshot=self.pair[0]["material_snapshot_json"],
					order_snapshot=self.pair[0]["order_snapshot_json"],
					occurred_at=at,
				)

	def test_frozen_structural_commit_requires_explicit_option_and_independent_history(self):
		args = dict(
			expected_source=self.records[0].source,
			source_snapshot_digest=self.original.source_snapshot_digest,
			cost_center=self.root["cost_center"],
		)
		root = {k: self.root[k] for k in ANCHOR_ROW_FIELDS}
		rows = [reservation_record_row(r) for r in [*self.records, self.record]]
		with self.assertRaises(ValueError):
			verify_frozen_reservation_anchor_row(root, rows, **args)
		position = verify_frozen_reservation_anchor_row(root, rows, **args, allow_commitments=True)
		self.assertEqual(position.open_commitment, "4.00")
		with self.assertRaises(ValueError):
			self.verify(root=self.root | {"event_count": 2})

	def test_replay_is_linear_and_does_not_rewrite_supplied_evidence(self):
		records = [*self.records, self.record]
		pairs = [self.pair]
		for _ in range(5):
			_, pair, record = history_commitment(self.original, records, amount="1.00")
			records.append(record)
			pairs.append(pair)
		before = copy.deepcopy((records, pairs))
		with patch(
			"medops.common.reservation_commitment_history.verify_reservation_records",
			wraps=verify_reservation_records,
		) as replay:
			position = verify_reservation_action_records(self.original, records, [], pairs)
		self.assertEqual(replay.call_count, 2)
		self.assertEqual(position.open_commitment, "9.00")
		self.assertEqual(before, (records, pairs))
