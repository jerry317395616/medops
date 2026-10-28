from __future__ import annotations

import unittest
from typing import Any, cast

from medops.common.authorized_reservation_projection import verify_authorized_initial_reservation
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS
from medops.common.initial_reservation_posting import initial_reservation_documents
from tests.test_authorized_reservation_projection import evidence
from tests.test_material_source_snapshot import facts


class InitialReservationDocumentsTest(unittest.TestCase):
	def test_documents_match_independently_verifiable_decision(self):
		item, _, _, confirmation, decision = evidence()
		documents = initial_reservation_documents(
			confirmation, decision, occurred_at="2026-01-01 10:02:00.000000"
		)
		projection = {field: documents.root[field] for field in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}}
		position, _, checked = verify_authorized_initial_reservation(
			projection, [dict(documents.event)], confirmation, decision, current_facts=facts()
		)
		self.assertEqual((position.open_reservation, checked.actor), (item.amount, decision["actor"]))
		self.assertEqual(documents.root["reservation_key"], documents.event["reservation"])
		self.assertEqual((documents.root["status"], documents.root["revision"]), ("In Progress", 1))
		with self.assertRaises(TypeError):
			cast(Any, documents.root)["status"] = "Closed"

	def test_root_identity_is_stable_across_retry_time(self):
		_, _, _, confirmation, decision = evidence()
		first = initial_reservation_documents(
			confirmation, decision, occurred_at="2026-01-01 10:02:00.000000"
		)
		second = initial_reservation_documents(
			confirmation, decision, occurred_at="2026-01-01 10:03:00.000000"
		)
		self.assertEqual(first.root["name"], second.root["name"])
		self.assertEqual(first.event["name"], second.event["name"])
		self.assertNotEqual(first.event["event_digest"], second.event["event_digest"])

	def test_different_allocation_produces_different_root(self):
		_, _, _, first, first_decision = evidence()
		_, _, _, other, other_decision = evidence(allocation="allocation-b")
		result = initial_reservation_documents(
			first, first_decision, occurred_at="2026-01-01 10:02:00.000000"
		)
		alternate = initial_reservation_documents(
			other, other_decision, occurred_at="2026-01-01 10:02:00.000000"
		)
		self.assertNotEqual(result.root["name"], alternate.root["name"])

	def test_tampering_and_posting_before_decision_fail(self):
		_, _, _, first, second = evidence()
		for confirmation, decision, stamp in (
			(first, second | {"actor": "other@example.com"}, "2026-01-01 10:02:00.000000"),
			(first, second, "2026-01-01 10:00:00.000000"),
		):
			with self.subTest(stamp=stamp), self.assertRaises(ValueError):
				initial_reservation_documents(confirmation, decision, occurred_at=stamp)
