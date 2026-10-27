from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from medops.common.budget_reservation_storage import (
	ROW_FIELDS,
	reservation_record_from_row,
	reservation_record_row,
	verify_reservation_rows,
)
from tests.test_budget_reservation_proof import record


class ReservationStorageContractTest(unittest.TestCase):
	def setUp(self):
		self.first = record(1, "Reserve", "10.00")
		self.second = record(2, "Commit", "6.00", previous=self.first.event_digest)
		self.rows = [reservation_record_row(self.first), reservation_record_row(self.second)]

	def verify(self, rows=None, **changes):
		values: dict[str, Any] = dict(
			expected_reservation="reservation-1", expected_count=2, expected_digest=self.second.event_digest
		)
		values.update(changes)
		return verify_reservation_rows(self.rows if rows is None else rows, **values)

	def test_record_row_roundtrip_accepts_native_database_date_and_datetime(self):
		row = dict(
			self.rows[0],
			period_start=date(2026, 1, 1),
			period_end=date(2026, 1, 31),
			occurred_at=datetime(2026, 10, 27, 9, 30),
			project="",
			previous_digest="",
		)
		self.assertEqual(reservation_record_from_row(row), self.first)
		position = self.verify([row, self.rows[1]])
		self.assertEqual((position.open_reservation, position.open_commitment), ("4.00", "6.00"))

	def test_projection_matches_runtime_doctype_fields(self):
		path = (
			Path(__file__).resolve().parents[1]
			/ "medops/hrp_budget/doctype/hrp_budget_reservation_event/hrp_budget_reservation_event.json"
		)
		meta = json.loads(path.read_text(encoding="utf-8"))
		self.assertEqual(ROW_FIELDS, {field["fieldname"] for field in meta["fields"]} | {"name"})
		self.assertEqual(set(self.rows[0]), ROW_FIELDS)

	def test_missing_extra_or_wrong_version_fields_are_rejected(self):
		missing = dict(self.rows[0])
		missing.pop("source_line")
		for row in (
			missing,
			dict(self.rows[0], extra="x"),
			dict(self.rows[0], proof_version=2),
			dict(self.rows[0], proof_version=True),
			dict(self.rows[0], name="other"),
		):
			with self.subTest(row=row), self.assertRaises(ValueError):
				reservation_record_from_row(row)

	def test_tampered_fields_and_evidence_are_rejected(self):
		for changes in (
			{"amount": "9.00"},
			{"source_action": "other"},
			{"actor": "other@example.com"},
			{"sequence_key": "0" * 64},
			{"event_digest": "0" * 64},
			{"company": " C "},
			{"period_start": "20260101"},
			{"occurred_at": "2026-10-27 09:30:00+00:00"},
			{"occurred_at": "2026-10-27T09:30:00.000000"},
			{"occurred_at": "2026-10-27"},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				reservation_record_from_row(dict(self.rows[0], **changes))

	def test_anchor_rejects_missing_tail_reordered_rows_and_wrong_identity(self):
		for rows, changes in (
			(self.rows[:1], {}),
			(list(reversed(self.rows)), {}),
			(self.rows, {"expected_reservation": "other"}),
			(self.rows, {"expected_digest": "0" * 64}),
			(self.rows, {"expected_count": True}),
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				self.verify(rows, **changes)

	def test_oversized_chain_is_rejected_before_decoding_rows(self):
		with patch(
			"medops.common.budget_reservation_storage.reservation_record_from_row",
			side_effect=AssertionError("oversized rows must not be decoded"),
		):
			with self.assertRaises(ValueError):
				self.verify([self.rows[0]] * 10001, expected_count=10001)


if __name__ == "__main__":
	unittest.main()
