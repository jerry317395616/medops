from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from medops.common.budget_control import budget_control_key
from medops.common.domain_service import canonical_json
from medops.common.material_source_snapshot import material_source_snapshot
from medops.common.reservation_authorization import (
	parse_reservation_intent_snapshot,
	reservation_authorization_row,
	reservation_confirmation,
	reservation_decision,
	reservation_intent,
	reservation_intent_snapshot,
	verify_reservation_authorization_rows,
	verify_reservation_confirmation_proof,
	verify_reservation_decision_evidence,
)
from tests.test_budget_control import key
from tests.test_material_source_snapshot import facts

PROOF = "synthetic_confirmation_proof_1234567890"
START = "2026-01-01 10:00:00.000000"
EXPIRES = "2026-01-01 10:05:00.000000"
DECIDED = "2026-01-01 10:01:00.000000"
SESSION = "a" * 64
ACTOR = "manager@example.com"


def intent(**changes):
	snapshot, digest = material_source_snapshot(facts())
	values = dict(
		source_snapshot=snapshot,
		source_snapshot_digest=digest,
		allocation="allocation-a",
		key=budget_control_key(key(company="Company", hospital="Hospital", organization_unit="Unit")),
		currency="CNY",
		precision=2,
		amount="10.00",
		budget_date="2026-01-01",
		reason="采购申请确认",
	)
	return reservation_intent(**(values | changes))


def confirmation(item, *, proof=PROOF, actor=ACTOR, session=SESSION, recorded=START, expires=EXPIRES):
	return reservation_confirmation(
		item,
		actor=actor,
		proof_hash=hashlib.sha256(proof.encode()).hexdigest(),
		session_digest=session,
		recorded_at=recorded,
		expires_at=expires,
	)


class ReservationAuthorizationTest(unittest.TestCase):
	def test_intent_binds_exact_source_budget_amount_date_and_reason(self):
		item = intent()
		for changes in (
			{"amount": "11.00"},
			{"reason": "另一原因"},
			{"allocation": "allocation-b"},
			{"budget_date": "2026-01-02"},
		):
			with self.subTest(changes=changes):
				self.assertNotEqual(item.intent_digest, intent(**changes).intent_digest)
		self.assertNotIn("采购申请确认", repr(item))
		self.assertNotIn("MR-001", repr(item))

	def test_intent_rejects_wrong_scope_currency_date_amount_or_input_form(self):
		wrong_key = budget_control_key(key(company="Other", hospital="Hospital", organization_unit="Unit"))
		for changes in (
			{"key": wrong_key},
			{"currency": "USD"},
			{"precision": True},
			{"amount": "10"},
			{"amount": "31.00"},
			{"amount": "0.00"},
			{"budget_date": "2026-02-01"},
			{"allocation": " allocation-a "},
			{"reason": " 原因 "},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				intent(**changes)

	def test_valid_confirmation_and_explicit_decision_are_bound(self):
		item = intent()
		first = confirmation(item)
		verify_reservation_confirmation_proof(
			item, first, proof=PROOF, actor=ACTOR, session_digest=SESSION, now=DECIDED
		)
		decision = reservation_decision(
			item,
			first,
			proof=PROOF,
			actor=ACTOR,
			session_digest=SESSION,
			recorded_at=DECIDED,
			acknowledge=True,
		)
		self.assertEqual(
			(decision.confirmation, decision.intent_digest, decision.source_snapshot_digest),
			(first.name, item.intent_digest, item.source_snapshot_digest),
		)
		self.assertEqual(
			decision,
			reservation_decision(
				item,
				first,
				proof=PROOF,
				actor=ACTOR,
				session_digest=SESSION,
				recorded_at=DECIDED,
				acknowledge=True,
			),
		)

	def test_confirmation_proof_actor_session_and_time_cannot_be_replaced(self):
		item = intent()
		first = confirmation(item)
		for changes in (
			{"proof": "different_confirmation_proof_1234567890"},
			{"actor": "other@example.com"},
			{"session_digest": "b" * 64},
			{"now": EXPIRES},
			{"now": "2026-01-01 09:59:59.000000"},
		):
			args = dict(proof=PROOF, actor=ACTOR, session_digest=SESSION, now=DECIDED) | changes
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				verify_reservation_confirmation_proof(item, first, **args)

	def test_confirmation_cannot_be_reused_for_changed_source_or_amount(self):
		item = intent()
		first = confirmation(item)
		snapshot, digest = material_source_snapshot(
			facts(status="Ordered", request_modified="2026-01-02 09:00:00")
		)
		updated = intent(source_snapshot=snapshot, source_snapshot_digest=digest)
		self.assertEqual(item.reservation_identity, updated.reservation_identity)
		self.assertNotEqual(item.intent_digest, updated.intent_digest)
		for changed in (updated, intent(amount="11.00")):
			with self.assertRaises(ValueError):
				verify_reservation_confirmation_proof(
					changed, first, proof=PROOF, actor=ACTOR, session_digest=SESSION, now=DECIDED
				)

	def test_forged_intent_or_confirmation_fields_are_detected(self):
		item = intent()
		first = confirmation(item)
		for changed in (
			replace(first, actor="other@example.com"),
			replace(first, intent_digest="0" * 64),
			replace(first, source_snapshot_digest="0" * 64),
			replace(first, event_digest="0" * 64),
		):
			with self.assertRaises(ValueError):
				verify_reservation_confirmation_proof(
					item, changed, proof=PROOF, actor=ACTOR, session_digest=SESSION, now=DECIDED
				)
		with self.assertRaises(ValueError):
			confirmation(replace(item, amount="11.00"))

	def test_decision_requires_acknowledgement_and_time_window(self):
		item = intent()
		first = confirmation(item)
		for acknowledgment, stamp in ((False, DECIDED), (1, DECIDED), (True, EXPIRES)):
			with self.subTest(acknowledgment=acknowledgment, stamp=stamp), self.assertRaises(ValueError):
				reservation_decision(
					item,
					first,
					proof=PROOF,
					actor=ACTOR,
					session_digest=SESSION,
					recorded_at=stamp,
					acknowledge=acknowledgment,
				)
		with self.assertRaises(ValueError):
			confirmation(item, expires="2026-01-01 10:05:01.000000")

	def test_one_time_key_is_stable_for_source_allocation_across_previews(self):
		item = intent()
		first = confirmation(item)
		other_proof = "another_confirmation_proof_1234567890"
		second = confirmation(item, proof=other_proof)
		a = reservation_decision(
			item,
			first,
			proof=PROOF,
			actor=ACTOR,
			session_digest=SESSION,
			recorded_at=DECIDED,
			acknowledge=True,
		)
		b = reservation_decision(
			item,
			second,
			proof=other_proof,
			actor=ACTOR,
			session_digest=SESSION,
			recorded_at=DECIDED,
			acknowledge=True,
		)
		self.assertNotEqual(first.name, second.name)
		self.assertNotEqual(a.name, b.name)
		self.assertEqual(a.one_time_key, b.one_time_key)
		other = intent(allocation="allocation-b")
		other_confirmation = confirmation(other)
		other_decision = reservation_decision(
			other,
			other_confirmation,
			proof=PROOF,
			actor=ACTOR,
			session_digest=SESSION,
			recorded_at=DECIDED,
			acknowledge=True,
		)
		self.assertNotEqual(a.one_time_key, other_decision.one_time_key)

	def test_historical_decision_verifies_without_plaintext_proof(self):
		item = intent()
		first = confirmation(item)
		decision = reservation_decision(
			item,
			first,
			proof=PROOF,
			actor=ACTOR,
			session_digest=SESSION,
			recorded_at=DECIDED,
			acknowledge=True,
		)
		self.assertEqual(verify_reservation_decision_evidence(item, first, decision), decision)
		self.assertNotIn(PROOF, repr(decision))

	def test_historical_decision_rejects_forged_fields_and_missing_acknowledgement(self):
		item = intent()
		first = confirmation(item)
		decision = reservation_decision(
			item,
			first,
			proof=PROOF,
			actor=ACTOR,
			session_digest=SESSION,
			recorded_at=DECIDED,
			acknowledge=True,
		)
		for changed in (
			replace(decision, acknowledged=False),
			replace(decision, actor="other@example.com"),
			replace(decision, confirmation="0" * 64),
			replace(decision, one_time_key="0" * 64),
			replace(decision, source_snapshot_digest="0" * 64),
			replace(decision, event_digest="0" * 64),
			replace(decision, recorded_at=EXPIRES),
		):
			with self.subTest(changed=changed), self.assertRaises(ValueError):
				verify_reservation_decision_evidence(item, first, changed)

	def test_persisted_intent_roundtrip_keeps_source_snapshot_separate(self):
		item = intent()
		payload = reservation_intent_snapshot(item)
		self.assertNotIn("MR-001", payload)
		self.assertEqual(
			parse_reservation_intent_snapshot(payload, source_snapshot=item.source_snapshot), item
		)

	def test_persisted_intent_rejects_changed_amount_digest_and_budget_scope(self):
		item = intent()
		payload = reservation_intent_snapshot(item)
		for change in (
			lambda row: row.update(amount="11.00"),
			lambda row: row.update(intent_digest="0" * 64),
			lambda row: row["budget_key"].update(organization_unit="Other"),
		):
			row = json.loads(payload)
			change(row)
			with self.assertRaises(ValueError):
				parse_reservation_intent_snapshot(canonical_json(row), source_snapshot=item.source_snapshot)

	def test_persisted_intent_rejects_wrong_source_and_unknown_or_duplicate_fields(self):
		item = intent()
		payload = reservation_intent_snapshot(item)
		other_source, _ = material_source_snapshot(facts(amount="31.000000000"))
		with self.assertRaises(ValueError):
			parse_reservation_intent_snapshot(payload, source_snapshot=other_source)
		row = json.loads(payload)
		row["extra"] = "untrusted"
		for candidate in (
			canonical_json(row),
			payload + " ",
			payload.replace('"schema_version":1', '"schema_version":1,"schema_version":1'),
		):
			with self.assertRaises(ValueError):
				parse_reservation_intent_snapshot(candidate, source_snapshot=item.source_snapshot)

	def test_persisted_intent_rejects_noncanonical_budget_key(self):
		item = intent()
		row = json.loads(reservation_intent_snapshot(item))
		row["budget_key"]["project"] = ""
		with self.assertRaises(ValueError):
			parse_reservation_intent_snapshot(canonical_json(row), source_snapshot=item.source_snapshot)

	def evidence_rows(self):
		item = intent()
		first = confirmation(item)
		decision = reservation_decision(
			item,
			first,
			proof=PROOF,
			actor=ACTOR,
			session_digest=SESSION,
			recorded_at=DECIDED,
			acknowledge=True,
		)
		return (
			item,
			decision,
			reservation_authorization_row(item, first),
			reservation_authorization_row(item, first, decision),
		)

	def test_stored_pair_roundtrips_database_dates_and_empty_optional_fields(self):
		item, decision, first, second = self.evidence_rows()
		for row in (first, second):
			row["recorded_at"] = datetime.fromisoformat(str(row["recorded_at"]))
			row["expires_at"] = str(row["expires_at"]).removesuffix(".000000")
		first["confirmation"] = ""
		first["one_time_key"] = ""
		self.assertEqual(verify_reservation_authorization_rows(first, second), (item, decision))
		self.assertNotIn(PROOF, repr((first, second)))

	def test_stored_decision_rejects_changed_bindings(self):
		_, _, first, second = self.evidence_rows()
		for field, value in (
			("actor", "other@example.com"),
			("intent_digest", "0" * 64),
			("source_snapshot_digest", "0" * 64),
			("budget_key_digest", "0" * 64),
			("confirmation", "0" * 64),
			("acknowledged", 0),
			("one_time_key", "0" * 64),
			("event_digest", "0" * 64),
			("source_snapshot_json", "{}"),
			("intent_snapshot_json", "{}"),
		):
			with self.subTest(field=field), self.assertRaises(ValueError):
				verify_reservation_authorization_rows(first, second | {field: value})

	def test_stored_confirmation_rejects_changed_bindings(self):
		_, _, first, second = self.evidence_rows()
		for field, value in (
			("actor", "other@example.com"),
			("source_name", "other"),
			("organization_unit", "other"),
			("proof_hash", "0" * 64),
			("session_digest", "0" * 64),
			("recorded_at", "2026-01-01 10:00:01"),
			("proof_version", 2),
			("evidence_key", "other"),
			("unknown", "value"),
		):
			with self.subTest(field=field), self.assertRaises(ValueError):
				verify_reservation_authorization_rows(first | {field: value}, second)

	def test_stored_pair_rejects_decision_from_another_confirmation(self):
		item, _, first, _ = self.evidence_rows()
		other = confirmation(item, proof="another_confirmation_proof_1234567890")
		decision = reservation_decision(
			item,
			other,
			proof="another_confirmation_proof_1234567890",
			actor=ACTOR,
			session_digest=SESSION,
			recorded_at=DECIDED,
			acknowledge=True,
		)
		with self.assertRaises(ValueError):
			verify_reservation_authorization_rows(first, reservation_authorization_row(item, other, decision))

	def test_stored_pair_rejects_noncanonical_time_and_boolean_flags(self):
		_, _, first, second = self.evidence_rows()
		for field, value in (
			("recorded_at", "2026-01-01T10:00:00"),
			("recorded_at", datetime(2026, 1, 1, tzinfo=timezone.utc)),
			("proof_version", True),
			("acknowledged", False),
		):
			with self.subTest(field=field), self.assertRaises(ValueError):
				verify_reservation_authorization_rows(first | {field: value}, second)

	def test_projection_rejects_forged_intent_or_decision(self):
		item, decision, _, _ = self.evidence_rows()
		first = confirmation(item)
		with self.assertRaises(ValueError):
			reservation_authorization_row(replace(item, amount="11.00"), first)
		with self.assertRaises(ValueError):
			reservation_authorization_row(item, first, replace(decision, acknowledged=False))
