from __future__ import annotations

import unittest
from dataclasses import asdict

from medops.common.material_source_snapshot import material_source_snapshot
from medops.common.reservation_recovery import build_reservation_recovery
from tests.test_reservation_authorization import intent


class ReservationRecoveryTest(unittest.TestCase):
	def payload(self):
		item = intent()
		return {
			"source_snapshot": item.source_snapshot,
			"source_snapshot_digest": item.source_snapshot_digest,
			"budget_key": asdict(item.key),
		}

	def test_exact_query_has_no_proof_or_write_authority(self):
		payload = self.payload()
		query = build_reservation_recovery(payload)
		self.assertEqual((query.offset, query.limit, query.expected_digest), (0, 20, None))
		self.assertNotIn(query.source_snapshot, repr(query))
		self.assertFalse(hasattr(query, "proof"))
		self.assertEqual(query.key, intent().key)

	def test_unknown_credentials_digest_and_scope_are_rejected(self):
		payload = self.payload()
		for value in (
			dict(payload, proof="must-not-accept"),
			dict(payload, source_snapshot_digest="f" * 64),
			dict(payload, budget_key=dict(payload["budget_key"], organization_unit="foreign")),
		):
			with self.assertRaises(ValueError):
				build_reservation_recovery(value)

	def test_bounds_reject_boolean_and_page_drift_without_digest(self):
		for extra in (
			{"offset": True},
			{"limit": True},
			{"offset": 101},
			{"limit": 21},
			{"limit": 0},
			{"offset": 20},
		):
			with self.assertRaises(ValueError):
				build_reservation_recovery(dict(self.payload(), **extra))
		query = build_reservation_recovery(dict(self.payload(), offset=20, expected_digest="a" * 64))
		self.assertEqual((query.offset, query.expected_digest), (20, "a" * 64))

	def test_source_change_does_not_silently_reuse_an_old_digest(self):
		from medops.common.material_source_snapshot import parse_material_source_snapshot

		payload = self.payload()
		facts = parse_material_source_snapshot(
			payload["source_snapshot"], expected_digest=payload["source_snapshot_digest"]
		)
		facts["amount"] = "1.000000000"
		changed, digest = material_source_snapshot(facts)
		with self.assertRaises(ValueError):
			build_reservation_recovery(dict(payload, source_snapshot=changed))
		self.assertEqual(
			build_reservation_recovery(
				dict(payload, source_snapshot=changed, source_snapshot_digest=digest)
			).source_snapshot_digest,
			digest,
		)
