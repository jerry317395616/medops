import unittest
from datetime import date, datetime

from medops.common.core_reports import (
	elapsed_minutes,
	normalized_duplicate_value,
	quality_trend,
	reference_batch,
	reference_identity,
	report_filters,
)


class TestCoreReportContracts(unittest.TestCase):
	def test_filter_bounds_and_unknown_filters(self):
		for value in (
			{"hospital": "H"},
			{"organization_unit": "U"},
			{"from_date": "2026-01-01"},
			{"from_date": "2020-01-01", "to_date": "2026-01-01"},
			{"user": "Administrator"},
			{"_medops_export": True},
		):
			with self.assertRaises(ValueError):
				report_filters(value)
		self.assertEqual(report_filters({"company": "C", "hospital": "H"}), {"company": "C", "hospital": "H"})

	def test_registration_has_an_explicit_scope_unique_targets_and_bounded_batch(self):
		scope = {"company": "C", "hospital": "H", "master_data_domain": "D"}
		entry = {"target_name": "001A", "valid_from": "2026-01-01"}
		self.assertEqual(reference_batch(scope, [entry])["entries"][0]["target_name"], "001A")
		for rows in ([], [entry, entry], [entry] * 101, [{**entry, "sql": "select 1"}]):
			with self.assertRaises(ValueError):
				reference_batch(scope, rows)

	def test_reference_identity_cannot_cross_hospitals(self):
		self.assertNotEqual(
			reference_identity({"company": "C", "hospital": "A", "target_name": "001"}),
			reference_identity({"company": "C", "hospital": "B", "target_name": "001"}),
		)

	def test_duplicate_normalization_keeps_business_text_but_unifies_width_and_case(self):
		self.assertEqual(
			normalized_duplicate_value("  ＡＢＣ　药品  "), normalized_duplicate_value("abc 药品")
		)
		self.assertNotEqual(normalized_duplicate_value("药品甲"), normalized_duplicate_value("药品乙"))

	def test_turnaround_is_actual_elapsed_time_and_rejects_negative_chronology(self):
		self.assertEqual(elapsed_minutes(datetime(2026, 1, 1, 0), datetime(2026, 1, 1, 0, 2)), 2)
		self.assertIsNone(elapsed_minutes(None, datetime(2026, 1, 1)))
		with self.assertRaises(ValueError):
			elapsed_minutes(datetime(2026, 1, 2), datetime(2026, 1, 1))

	def test_trend_distinguishes_unknown_baseline_current_and_future_days(self):
		rows = quality_trend(
			[{"day": "2026-01-02", "baseline_open": 2, "resolved": 1}],
			start=date(2026, 1, 1),
			end=date(2026, 1, 3),
			epoch=datetime(2026, 1, 2),
			observed_until=date(2026, 1, 2),
		)
		self.assertIsNone(rows[0]["open_at_end"])
		self.assertEqual(rows[1]["open_at_end"], 1)
		self.assertEqual(rows[1]["coverage"], "Recording")
		self.assertIsNone(rows[2]["open_at_end"])
		with self.assertRaises(ValueError):
			quality_trend(
				[{"day": "2026-01-02", "resolved": 2}],
				start=date(2026, 1, 2),
				end=date(2026, 1, 2),
				epoch=datetime(2026, 1, 2),
			)
