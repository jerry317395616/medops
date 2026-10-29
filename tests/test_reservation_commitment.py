from __future__ import annotations

import json
import unittest
from dataclasses import replace

from medops.common.budget_reservation_events import ReservationEvent
from medops.common.budget_reservation_proof import (
	ReservationSource,
	reservation_event_key,
	reservation_record,
)
from medops.common.domain_service import canonical_json, fingerprint_json
from medops.common.material_source_snapshot import material_source_snapshot
from medops.common.reservation_commitment import (
	parse_purchase_order_snapshot,
	purchase_order_snapshot,
	reservation_commitment_intent,
)
from tests.test_material_source_snapshot import facts
from tests.test_reservation_authorization import intent


def order_facts(digest, **changes):
	return (
		dict(
			order="PO-001",
			line="PO-LINE-001",
			order_modified="2026-01-02 10:00:00",
			line_modified="2026-01-02 10:00:00",
			material_request="MR-001",
			material_request_line="MR-LINE-001",
			material_request_digest=digest,
			company="Company",
			hospital="Hospital",
			organization_unit="Unit",
			cost_center="Center",
			expense_account="Account",
			project=None,
			supplier="Supplier",
			currency="CNY",
			transaction_date="2026-01-02",
			status="To Receive and Bill",
			item_code="Item",
			uom="Unit",
			quantity="2.000000000",
			rate="6.000000000",
			amount="12.000000000",
			base_amount="12.000000000",
		)
		| changes
	)


def record(original, previous=(), *, kind="Reserve", amount=None, source=None):
	sequence = len(previous) + 1
	name = original.reservation_identity
	event = ReservationEvent(
		reservation_event_key(name, sequence),
		name,
		sequence,
		original.key,
		original.currency,
		original.precision,
		kind,
		amount or original.amount,
	)
	source = source or ReservationSource(
		"Material Request",
		"MR-001",
		original.source_snapshot_digest,
		"MR-LINE-001",
		original.allocation,
		"Reserve" if sequence == 1 else f"Synthetic structural action:{sequence}",
	)
	return reservation_record(
		event,
		source,
		actor="manager@example.com",
		occurred_at=f"2026-01-02 11:00:{sequence:02}.000000",
		previous_digest=previous[-1].event_digest if previous else None,
	)


class ReservationCommitmentTest(unittest.TestCase):
	def setUp(self):
		self.original = intent()
		self.records = [record(self.original)]
		self.material, self.material_digest = material_source_snapshot(facts())

	def snapshot(self, **changes):
		return purchase_order_snapshot(
			order_facts(self.material_digest, **changes),
			material_snapshot=self.material,
			material_digest=self.material_digest,
		)

	def proposed(
		self,
		*,
		records=None,
		original=None,
		amount: object = "6.00",
		reason="Synthetic commitment review",
		order_amount=None,
		**changes,
	):
		if order_amount is not None:
			changes["amount"] = order_amount
		text, digest = self.snapshot(**changes)
		return reservation_commitment_intent(
			original or self.original,
			self.records if records is None else records,
			order_snapshot=text,
			order_digest=digest,
			material_snapshot=self.material,
			material_digest=self.material_digest,
			amount=amount,
			reason=reason,
		)

	def test_order_snapshot_preserves_native_digest_and_stable_line_identity(self):
		text, digest = self.snapshot()
		self.assertEqual(
			digest,
			fingerprint_json(
				{"schema_version": 1, "kind": "purchase_order_source", **order_facts(self.material_digest)}
			),
		)
		self.assertEqual(
			parse_purchase_order_snapshot(
				text,
				expected_digest=digest,
				material_snapshot=self.material,
				material_digest=self.material_digest,
			),
			order_facts(self.material_digest),
		)
		first = self.proposed()
		second = self.proposed(status="To Bill", order_modified="2026-01-03 10:00:00")
		self.assertEqual(first.commitment_identity, second.commitment_identity)
		self.assertNotEqual(first.intent_digest, second.intent_digest)

	def test_order_parser_rejects_noncanonical_duplicate_forged_and_oversized_input(self):
		text, digest = self.snapshot()
		for value, expected in (
			(json.dumps(json.loads(text), indent=2), digest),
			(text[:-1] + ',"schema_version":1}', digest),
			(text, "f" * 64),
			("x" * 32769, digest),
		):
			with self.subTest(value_length=len(value)), self.assertRaises(ValueError):
				parse_purchase_order_snapshot(
					value,
					expected_digest=expected,
					material_snapshot=self.material,
					material_digest=self.material_digest,
				)
		for patch in ({"schema_version": True}, {"kind": "other"}, {"unknown": "x"}):
			data = json.loads(text) | patch
			with self.assertRaises(ValueError):
				parse_purchase_order_snapshot(
					canonical_json(data),
					expected_digest=fingerprint_json(data),
					material_snapshot=self.material,
					material_digest=self.material_digest,
				)

	def test_purchase_source_link_scope_money_status_and_time_are_strict(self):
		for change in (
			{"material_request": "other"},
			{"material_request_line": "other"},
			{"material_request_digest": "a" * 64},
			{"company": "other"},
			{"hospital": "other"},
			{"organization_unit": "other"},
			{"cost_center": "other"},
			{"expense_account": "other"},
			{"project": "other"},
			{"item_code": "other"},
			{"uom": "other"},
			{"currency": "USD"},
			{"quantity": "6.000000000"},
			{"quantity": "0.000000000"},
			{"amount": 12.0},
			{"amount": "12"},
			{"base_amount": "13.000000000"},
			{"status": "Cancelled"},
			{"order_modified": "2026-01-02T10:00:00Z"},
			{"transaction_date": "2026-1-2"},
			{"supplier": " Supplier"},
		):
			with self.subTest(change=change), self.assertRaises(ValueError):
				self.snapshot(**change)

	def test_exact_commitment_binds_original_order_and_current_predecessor(self):
		value = self.proposed()
		self.assertEqual(
			(value.open_before, value.order_amount, value.amount), ("10.00", "12.000000000", "6.00")
		)
		self.assertEqual(value.previous_event_digest, self.records[-1].event_digest)
		self.assertEqual(value.original_intent_digest, self.original.intent_digest)
		self.assertNotEqual(value.intent_digest, self.proposed(amount="5.00").intent_digest)
		self.assertNotEqual(
			value.intent_digest, self.proposed(reason="Different independent review").intent_digest
		)
		self.assertNotIn("Synthetic commitment review", repr(value))

	def test_current_request_progress_preserves_original_economics_and_identity(self):
		self.material, self.material_digest = material_source_snapshot(
			facts(status="Ordered", request_modified="2026-01-03 10:00:00")
		)
		value = self.proposed()
		self.assertEqual(value.original_source_digest, self.original.source_snapshot_digest)
		self.assertNotEqual(value.current_source_digest, value.original_source_digest)
		self.material, self.material_digest = material_source_snapshot(facts(rate="7.000000000"))
		with self.assertRaises(ValueError):
			self.proposed()

	def test_structural_partial_release_and_prior_commit_reduce_remaining_reservation(self):
		# These constructed records prove replay math, not independent financial authority.
		self.records.append(record(self.original, self.records, kind="Release Reservation", amount="4.00"))
		self.assertEqual(self.proposed().open_before, "6.00")
		with self.assertRaises(ValueError):
			self.proposed(amount="6.01")
		self.records.append(record(self.original, self.records, kind="Commit", amount="2.00"))
		self.assertEqual(self.proposed(amount="4.00").open_before, "4.00")
		with self.assertRaises(ValueError):
			self.proposed(amount="4.01")

	def test_missing_forged_cross_source_and_extra_reserve_history_cannot_bind(self):
		for records in ([], [replace(self.records[0], event_digest="f" * 64)]):
			with self.assertRaises(ValueError):
				self.proposed(records=records)
		other_source = replace(self.records[0].source, line="other")
		with self.assertRaises(ValueError):
			self.proposed(records=[record(self.original, source=other_source)])
		self.records.append(record(self.original, self.records, kind="Reserve", amount="1.00"))
		with self.assertRaises(ValueError):
			self.proposed()

	def test_amount_order_limit_terminal_state_period_and_canonical_intent_fail_closed(self):
		for value in (0, 1.0, "6", "0.00", "-1.00", "10.01", "NaN"):
			with self.subTest(value=value), self.assertRaises(ValueError):
				self.proposed(amount=value)
		with self.assertRaises(ValueError):
			self.proposed(
				amount="9.00", order_amount="8.000000000", base_amount="8.000000000", rate="4.000000000"
			)
		with self.assertRaises(ValueError):
			self.proposed(transaction_date="2026-02-01")
		with self.assertRaises(ValueError):
			self.proposed(reason=" trailing ")
		self.records.append(record(self.original, self.records, kind="Release Reservation", amount="10.00"))
		with self.assertRaises(ValueError):
			self.proposed(amount="0.01")
