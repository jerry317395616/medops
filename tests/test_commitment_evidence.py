from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from datetime import date, datetime

from medops.common.commitment_evidence import (
	COMMITMENT_ROW_FIELDS,
	commitment_confirmation,
	commitment_decision,
	commitment_evidence_row,
	verify_commitment_evidence,
	verify_commitment_evidence_row,
	verify_commitment_proof,
)
from medops.common.domain_service import canonical_json
from medops.common.reservation_commitment import (
	parse_reservation_commitment_snapshot,
	require_commitment_intent,
	reservation_commitment_snapshot,
)
from medops.tests.commitment_evidence_fixtures import (
	ACTOR,
	DECIDED,
	END,
	PROOF,
	SESSION,
	START,
	commitment_fixture,
	evidence_fixture,
)


class CommitmentEvidenceTest(unittest.TestCase):
	def setUp(self):
		self.item, self.first, self.second, self.material, self.order = evidence_fixture()

	def row(self, *, decision=False):
		return commitment_evidence_row(
			self.item,
			self.second if decision else self.first,
			material_snapshot=self.material,
			order_snapshot=self.order,
			confirmed=self.first if decision else None,
		)

	def decision(self, **changes):
		args = (
			dict(proof=PROOF, actor=ACTOR, session_digest=SESSION, recorded_at=DECIDED, acknowledge=True)
			| changes
		)
		return commitment_decision(self.item, self.first, **args)

	def test_v2_intent_snapshot_roundtrip_rejects_unpersisted_v1_and_unknown_fields(self):
		text = reservation_commitment_snapshot(self.item)
		self.assertEqual(parse_reservation_commitment_snapshot(text), self.item)
		for patch in ({"schema_version": 1}, {"schema_version": True}, {"extra": 1}):
			with self.assertRaises(ValueError):
				parse_reservation_commitment_snapshot(canonical_json(json.loads(text) | patch))
		for bad in (text[:-1] + ',"schema_version":2}', json.dumps(json.loads(text), indent=2), "x" * 32769):
			with self.assertRaises(ValueError):
				parse_reservation_commitment_snapshot(bad)

	def test_intent_and_source_time_are_canonical_and_digest_bound(self):
		for changes in (
			{"amount": "5.00"},
			{"source_modified_at": "2026-01-02 10:00:00"},
			{"previous_sequence": True},
			{"order_snapshot_digest": "F" * 64},
			{"precision": True},
		):
			with self.assertRaises(ValueError):
				require_commitment_intent(replace(self.item, **changes))

	def test_confirmation_is_deterministic_without_acknowledgement_or_raw_proof(self):
		self.assertEqual(verify_commitment_evidence(self.item, self.first), self.first)
		self.assertEqual(
			(
				self.first.event_kind,
				self.first.acknowledged,
				self.first.confirmation,
				self.first.one_time_key,
			),
			("Confirmation", False, None, None),
		)
		self.assertNotIn(PROOF, canonical_json(self.row()))

	def test_confirmation_cannot_predate_tail_or_source_or_exceed_five_minutes(self):
		for start, end in (
			("2026-01-02 10:59:59.000000", END),
			(START, START),
			(START, "2026-01-02 11:06:00.000001"),
		):
			with self.assertRaises(ValueError):
				commitment_confirmation(
					self.item,
					actor=ACTOR,
					proof_hash=self.first.proof_hash,
					session_digest=SESSION,
					recorded_at=start,
					expires_at=end,
				)
		future, _, _ = commitment_fixture(order_changes={"order_modified": "2026-01-02 11:05:00"})
		with self.assertRaises(ValueError):
			commitment_confirmation(
				future,
				actor=ACTOR,
				proof_hash=self.first.proof_hash,
				session_digest=SESSION,
				recorded_at=START,
				expires_at=END,
			)

	def test_only_explicit_true_can_decide_and_same_request_has_same_evidence(self):
		self.assertEqual(self.decision(), self.second)
		for flag in (False, 0, 1, "true", None):
			with self.subTest(flag=flag), self.assertRaises(ValueError):
				self.decision(acknowledge=flag)
		self.assertEqual(self.second.confirmation, self.first.name)
		self.assertTrue(self.second.acknowledged and self.second.one_time_key)

	def test_proof_actor_session_and_exact_expiry_are_rechecked(self):
		verify_commitment_proof(
			self.item, self.first, proof=PROOF, actor=ACTOR, session_digest=SESSION, now=START
		)
		for changes in (
			{"proof": "x" * 40},
			{"proof": "\u00e9" * 40},
			{"actor": "other@example.com"},
			{"session_digest": "a" * 64},
			{"recorded_at": END},
			{"recorded_at": "2026-01-02 11:00:59.999999"},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				self.decision(**changes)

	def test_rebinding_confirmation_to_different_intent_or_order_is_denied(self):
		for item in (
			commitment_fixture(amount="5.00")[0],
			commitment_fixture(order_changes={"order": "PO-002"})[0],
		):
			with self.assertRaises(ValueError):
				verify_commitment_evidence(item, self.first)
		for changes in (
			{"actor": "other"},
			{"event_kind": "Decision"},
			{"acknowledged": True},
			{"event_digest": "f" * 64},
		):
			with self.assertRaises(ValueError):
				verify_commitment_evidence(self.item, replace(self.first, **changes))

	def test_same_root_predecessor_decisions_share_successor_even_if_review_changes(self):
		item, _, _ = commitment_fixture(amount="5.00")
		first = commitment_confirmation(
			item,
			actor=ACTOR,
			proof_hash=self.first.proof_hash,
			session_digest=SESSION,
			recorded_at=START,
			expires_at=END,
		)
		second = commitment_decision(
			item,
			first,
			proof=PROOF,
			actor=ACTOR,
			session_digest=SESSION,
			recorded_at=DECIDED,
			acknowledge=True,
		)
		self.assertNotEqual(second.name, self.second.name)
		self.assertEqual(second.one_time_key, self.second.one_time_key)

	def test_complete_rows_roundtrip_and_bind_scope_and_source_versions(self):
		for decision in (False, True):
			row = self.row(decision=decision)
			self.assertEqual(set(row), COMMITMENT_ROW_FIELDS)
			self.assertEqual(
				verify_commitment_evidence_row(row, self.first if decision else None),
				(self.item, self.second if decision else self.first),
			)
			self.assertEqual((row["purchase_order"], row["purchase_order_line"]), ("PO-001", "PO-LINE-001"))

	def test_native_date_time_and_nullable_values_normalize_without_changing_evidence(self):
		row = self.row(decision=True)
		row |= {
			name: datetime.fromisoformat(row[name])
			for name in ("recorded_at", "expires_at", "source_modified_at")
		}
		row |= {name: date.fromisoformat(row[name]) for name in ("period_start", "period_end")}
		row["project"] = ""
		self.assertEqual(verify_commitment_evidence_row(row, self.first), (self.item, self.second))

	def test_missing_extra_tampered_and_noncanonical_columns_fail_closed(self):
		row = self.row(decision=True)
		bad = [row | {"extra": "x"}, {k: v for k, v in row.items() if k != "actor"}]
		bad.extend(
			row | patch
			for patch in (
				{"company": "other"},
				{"purchase_order": "other"},
				{"budget_key_digest": "f" * 64},
				{"material_snapshot_json": "{}"},
				{"order_snapshot_json": "{}"},
				{"proof_version": True},
				{"acknowledged": 2},
				{"previous_sequence": 1.0},
				{"recorded_at": "2026-01-02T11:02:00Z"},
				{"period_start": datetime(2026, 1, 1)},
			)
		)
		for value in bad:
			with self.assertRaises(ValueError):
				verify_commitment_evidence_row(value, self.first)

	def test_private_proof_and_session_hashes_and_reason_are_absent_from_repr(self):
		self.assertEqual(self.first.proof_hash, hashlib.sha256(PROOF.encode()).hexdigest())
		for private in (PROOF, self.first.proof_hash, SESSION):
			self.assertNotIn(private, repr(self.first))
			self.assertNotIn(private, repr(self.second))
		self.assertNotIn(self.item.reason, repr(self.item))
