from __future__ import annotations

import json
import unittest

from medops.common.budget_control import budget_control_key
from medops.common.budget_reservation_anchor import reservation_anchor
from medops.common.budget_reservation_events import ReservationEvent
from medops.common.budget_reservation_proof import (
	ReservationSource,
	reservation_event_key,
	reservation_record,
)
from medops.common.budget_reservation_storage import reservation_record_row
from medops.common.domain_service import canonical_json, fingerprint_json
from medops.common.material_source_snapshot import (
	build_snapshot_reservation_projection,
	compare_material_source_history,
	material_source_snapshot,
	parse_material_source_snapshot,
	verify_snapshot_reservation_projection,
)
from tests.test_budget_control import key


def facts(**changes):
	values = {
		"request": "MR-001",
		"line": "MR-LINE-001",
		"request_modified": "2026-01-01 09:00:00",
		"line_modified": "2026-01-01 09:00:00",
		"company": "Company",
		"hospital": "Hospital",
		"organization_unit": "Unit",
		"organization_version": "Version",
		"mapping_revision": 1,
		"cost_center": "Center",
		"currency": "CNY",
		"transaction_date": "2026-01-01",
		"docstatus": 1,
		"status": "Pending",
		"item_code": "Item",
		"uom": "Unit",
		"expense_account": "Account",
		"project": None,
		"quantity": "5.000000000",
		"rate": "6.000000000",
		"amount": "30.000000000",
	}
	return values | changes


class MaterialSourceSnapshotTest(unittest.TestCase):
	def test_snapshot_digest_matches_existing_native_source_format(self):
		value = facts()
		payload, digest = material_source_snapshot(value)
		self.assertEqual(
			digest, fingerprint_json({"schema_version": 1, "kind": "material_request_source", **value})
		)
		self.assertEqual(parse_material_source_snapshot(payload, expected_digest=digest), value)

	def test_normal_order_progress_is_distinguished_from_financial_change(self):
		payload, digest = material_source_snapshot(facts())
		result = compare_material_source_history(
			payload,
			expected_digest=digest,
			current_facts=facts(
				status="Partially Ordered",
				request_modified="2026-01-02 09:00:00",
				line_modified="2026-01-02 09:00:00.123456",
			),
		)
		self.assertTrue(result.status_changed)
		self.assertTrue(result.timestamps_changed)
		self.assertNotEqual(result.historical_digest, result.current_digest)

	def test_source_modification_time_must_not_regress(self):
		payload, digest = material_source_snapshot(facts())
		for field in ("request_modified", "line_modified"):
			with self.subTest(field=field), self.assertRaises(ValueError):
				compare_material_source_history(
					payload,
					expected_digest=digest,
					current_facts=facts(**{field: "2026-01-01 08:59:59.999999"}),
				)
		# Different canonical precisions representing the same instant are allowed.
		result = compare_material_source_history(
			payload,
			expected_digest=digest,
			current_facts=facts(request_modified="2026-01-01 09:00:00.000000"),
		)
		self.assertTrue(result.timestamps_changed)

	def test_unchanged_snapshot_is_not_reported_as_progress(self):
		payload, digest = material_source_snapshot(facts())
		result = compare_material_source_history(payload, expected_digest=digest, current_facts=facts())
		self.assertFalse(result.status_changed or result.timestamps_changed)
		self.assertEqual(result.current_digest, digest)

	def test_any_financial_or_identity_fact_change_is_rejected(self):
		payload, digest = material_source_snapshot(facts())
		changes = {
			"request": "MR-002",
			"line": "another",
			"company": "Other",
			"hospital": "Other",
			"organization_unit": "Other",
			"organization_version": "Other",
			"mapping_revision": 2,
			"cost_center": "Other",
			"currency": "USD",
			"transaction_date": "2026-01-02",
			"item_code": "Other",
			"uom": "Other",
			"expense_account": "Other",
			"project": "Other",
			"quantity": "4.000000000",
			"rate": "7.000000000",
			"amount": "31.000000000",
		}
		for field, value in changes.items():
			with self.subTest(field=field), self.assertRaises(ValueError):
				compare_material_source_history(
					payload, expected_digest=digest, current_facts=facts(**{field: value})
				)

	def test_unsubmitted_stopped_and_cancelled_sources_are_not_accepted(self):
		for change in (
			{"docstatus": 0},
			{"docstatus": True},
			{"status": "Stopped"},
			{"status": "Cancelled"},
			{"status": "unknown"},
		):
			with self.subTest(change=change), self.assertRaises(ValueError):
				material_source_snapshot(facts(**change))

	def test_malformed_numbers_references_timestamps_and_fields_are_rejected(self):
		for change in (
			{"quantity": 5.0},
			{"quantity": "5"},
			{"quantity": "0.000000000"},
			{"amount": "0.000000000"},
			{"mapping_revision": True},
			{"expense_account": None},
			{"request": " MR-001"},
			{"request_modified": "2026-01-01T09:00:00"},
			{"extra": "x"},
		):
			with self.subTest(change=change), self.assertRaises(ValueError):
				material_source_snapshot(facts(**change))

	def test_digest_and_exact_canonical_representation_are_required(self):
		payload, digest = material_source_snapshot(facts())
		for candidate, expected in (
			(payload, "0" * 64),
			(payload + " ", digest),
			(payload.replace('"schema_version":1', '"schema_version":1,"schema_version":1'), digest),
			("{" + '"x":1,' * 5000 + '"x":1}', digest),
		):
			with self.subTest(length=len(candidate)), self.assertRaises(ValueError):
				parse_material_source_snapshot(candidate, expected_digest=expected)

	def test_unknown_and_missing_snapshot_fields_are_rejected_even_with_matching_digest(self):
		payload, _ = material_source_snapshot(facts())
		for mutate in (lambda value: value.pop("quantity"), lambda value: value.update(extra="x")):
			value = json.loads(payload)
			mutate(value)
			with self.assertRaises(ValueError):
				parse_material_source_snapshot(canonical_json(value), expected_digest=fingerprint_json(value))

	def reservation(self, *, amount="10.00"):
		original = facts()
		_, digest = material_source_snapshot(original)
		budget = budget_control_key(key(company="Company", hospital="Hospital", organization_unit="Unit"))
		source = ReservationSource(
			"Material Request", original["request"], digest, original["line"], "A", "Reserve"
		)
		event = ReservationEvent(
			reservation_event_key("reservation", 1), "reservation", 1, budget, "CNY", 2, "Reserve", amount
		)
		record = reservation_record(
			event,
			source,
			actor="user@example.com",
			occurred_at="2026-01-01 10:00:00.000000",
			previous_digest=None,
		)
		anchor = reservation_anchor(
			[record],
			expected_source=source,
			source_snapshot_digest=digest,
			cost_center=original["cost_center"],
		)
		return original, digest, budget, [record], anchor

	def test_snapshot_bearing_reservation_accepts_nonfinancial_progress(self):
		original, digest, budget, records, anchor = self.reservation()
		row = build_snapshot_reservation_projection(
			anchor, records, source_facts=original, allocation="A", expected_key=budget
		)
		position, comparison = verify_snapshot_reservation_projection(
			row,
			[reservation_record_row(r) for r in records],
			expected_snapshot_digest=digest,
			allocation="A",
			expected_key=budget,
			current_facts=facts(status="Ordered", request_modified="2026-01-02 09:00:00"),
		)
		self.assertEqual(position.open_reservation, "10.00")
		self.assertTrue(comparison.status_changed and comparison.timestamps_changed)

	def test_snapshot_reservation_rejects_changed_cost_or_request_amount(self):
		original, digest, budget, records, anchor = self.reservation()
		row = build_snapshot_reservation_projection(
			anchor, records, source_facts=original, allocation="A", expected_key=budget
		)
		for current in (facts(cost_center="Other"), facts(amount="31.000000000")):
			with self.assertRaises(ValueError):
				verify_snapshot_reservation_projection(
					row,
					[reservation_record_row(r) for r in records],
					expected_snapshot_digest=digest,
					allocation="A",
					expected_key=budget,
					current_facts=current,
				)

	def test_snapshot_reservation_requires_independent_digest_and_complete_events(self):
		original, digest, budget, records, anchor = self.reservation()
		row = build_snapshot_reservation_projection(
			anchor, records, source_facts=original, allocation="A", expected_key=budget
		)
		for expected, events in (("0" * 64, [reservation_record_row(r) for r in records]), (digest, [])):
			with self.assertRaises(ValueError):
				verify_snapshot_reservation_projection(
					row,
					events,
					expected_snapshot_digest=expected,
					allocation="A",
					expected_key=budget,
					current_facts=original,
				)

	def test_snapshot_projection_rejects_wrong_key_allocation_or_scope(self):
		original, _, budget, records, anchor = self.reservation()
		other_key = budget_control_key(key(company="Company", hospital="Hospital", organization_unit="Other"))
		for allocation, expected_key in (("B", budget), ("A", other_key)):
			with self.assertRaises(ValueError):
				build_snapshot_reservation_projection(
					anchor, records, source_facts=original, allocation=allocation, expected_key=expected_key
				)

	def test_snapshot_projection_rejects_exposure_above_historical_amount(self):
		original, _, budget, records, anchor = self.reservation(amount="31.00")
		with self.assertRaises(ValueError):
			build_snapshot_reservation_projection(
				anchor, records, source_facts=original, allocation="A", expected_key=budget
			)


if __name__ == "__main__":
	unittest.main()
