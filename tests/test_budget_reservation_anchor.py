from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from medops.common.budget_reservation_anchor import (
	ANCHOR_ROW_FIELDS,
	reservation_anchor,
	reservation_anchor_row,
	reservation_source_identity,
	verify_reservation_anchor,
	verify_reservation_anchor_row,
)
from medops.common.budget_reservation_storage import reservation_record_row
from tests.test_budget_reservation_proof import record, source


class ReservationAnchorContractTest(unittest.TestCase):
	def setUp(self):
		self.first = record(1, "Reserve", "10.00")
		self.second = record(2, "Commit", "6.00", previous=self.first.event_digest)
		self.records = [self.first, self.second]
		self.values: dict[str, Any] = dict(
			expected_source=self.first.source,
			source_snapshot_digest="a" * 64,
			cost_center="Department A - HC",
		)

	def test_anchor_reconstructs_exact_position_and_chain_tail(self):
		anchor = reservation_anchor(self.records, **self.values)
		position = verify_reservation_anchor(anchor, self.records, **self.values)
		self.assertEqual((position.reserved, position.committed), ("10.00", "6.00"))
		self.assertEqual((position.open_reservation, position.open_commitment), ("4.00", "6.00"))
		self.assertEqual(anchor.last_event_digest, self.second.event_digest)
		self.assertEqual(anchor, reservation_anchor(self.records, **self.values))

	def test_source_allocation_identity_is_stable_across_revision_and_action(self):
		original = source()
		identity = reservation_source_identity(original)
		self.assertEqual(identity, reservation_source_identity(source(revision="3", action="amend")))
		for changes in ({"name": "PR-2"}, {"line": "LINE-2"}, {"allocation": "B"}, {"doctype": "Other"}):
			with self.subTest(changes=changes):
				self.assertNotEqual(identity, reservation_source_identity(source(**changes)))

	def test_initial_source_and_snapshot_are_bound(self):
		anchor = reservation_anchor(self.records, **self.values)
		for values in (
			dict(self.values, expected_source=source(revision="3")),
			dict(self.values, expected_source=source(action="other")),
			dict(self.values, source_snapshot_digest="b" * 64),
			dict(self.values, cost_center="Department B - HC"),
		):
			with self.subTest(values=values), self.assertRaises(ValueError):
				verify_reservation_anchor(anchor, self.records, **values)

	def test_tampered_anchor_or_incomplete_chain_is_rejected(self):
		anchor = reservation_anchor(self.records, **self.values)
		for modified in (
			replace(anchor, anchor_digest="0" * 64),
			replace(anchor, source_identity_key="0" * 64),
			replace(anchor, last_event_digest=self.first.event_digest),
			replace(anchor, position=replace(anchor.position, open_reservation="5.00")),
		):
			with self.subTest(modified=modified), self.assertRaises(ValueError):
				verify_reservation_anchor(modified, self.records, **self.values)
		for records in (self.records[:1], list(reversed(self.records)), [self.first, self.first]):
			with self.subTest(records=records), self.assertRaises(ValueError):
				verify_reservation_anchor(anchor, records, **self.values)

	def test_invalid_snapshot_and_noncanonical_cost_center_are_rejected(self):
		for changes in (
			{"source_snapshot_digest": "A" * 64},
			{"source_snapshot_digest": "x"},
			{"cost_center": " Department A - HC "},
			{"cost_center": ""},
		):
			values: dict[str, Any] = dict(self.values)
			values.update(changes)
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				reservation_anchor(self.records, **values)

	def test_aggregate_projection_matches_real_columns_and_native_dates(self):
		anchor = reservation_anchor(self.records, **self.values)
		row = reservation_anchor_row(anchor)
		meta = json.loads(
			(
				Path(__file__).resolve().parents[1]
				/ "medops/hrp_budget/doctype/hrp_budget_reservation/hrp_budget_reservation.json"
			).read_text(encoding="utf-8")
		)
		self.assertTrue(ANCHOR_ROW_FIELDS <= ({field["fieldname"] for field in meta["fields"]} | {"name"}))
		self.assertEqual(set(row), ANCHOR_ROW_FIELDS)
		row.update(period_start=date(2026, 1, 1), period_end=date(2026, 1, 31), project="")
		position = verify_reservation_anchor_row(
			row, [reservation_record_row(value) for value in self.records], **self.values
		)
		self.assertEqual((position.open_reservation, position.open_commitment), ("4.00", "6.00"))

	def test_aggregate_projection_rejects_changed_amount_source_or_chain_anchor(self):
		row = reservation_anchor_row(reservation_anchor(self.records, **self.values))
		rows = [reservation_record_row(value) for value in self.records]
		for changes in (
			{"reserved_amount": "5.00"},
			{"committed_amount": "5.00"},
			{"actual_amount": "0.01"},
			{"source_revision": "3"},
			{"source_identity_key": "0" * 64},
			{"source_snapshot_digest": "0" * 64},
			{"last_event_digest": self.first.event_digest},
			{"event_count": True},
			{"currency_precision": True},
			{"proof_version": True},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				verify_reservation_anchor_row(row | changes, rows, **self.values)
		with self.assertRaises(ValueError):
			verify_reservation_anchor_row(row, rows[:1], **self.values)
		with self.assertRaises(ValueError):
			verify_reservation_anchor_row(row | {"extra": "unexpected"}, rows, **self.values)


if __name__ == "__main__":
	unittest.main()
