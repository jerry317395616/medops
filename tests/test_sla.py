from __future__ import annotations

import unittest
from datetime import datetime

from medops.common.sla import (
	MAX_SLA_DURATION_SECONDS,
	SLAClock,
	SLAContractError,
	build_sla_escalation_steps,
	build_sla_support_hours,
	normalize_sla_clock,
	normalize_sla_duration,
	normalize_sla_field,
	policy_payload,
	sla_digest,
	validate_sla_status_groups,
)


def _hours(day: str = "Monday") -> dict[str, str]:
	return {"workday": day, "start_time": "08:00:00", "end_time": "17:00:00"}


class SLAContractTests(unittest.TestCase):
	def test_working_hours_match_the_standard_erpnext_calendar_shape(self) -> None:
		hours = build_sla_support_hours([_hours("Friday"), _hours("Monday")])
		self.assertEqual([row.workday for row in hours], ["Monday", "Friday"])
		self.assertEqual(hours[0].as_erpnext_payload(), _hours())

	def test_ambiguous_or_executable_calendar_configuration_is_rejected(self) -> None:
		invalid = [
			[_hours(), _hours()],
			[{**_hours(), "condition": "doc.enabled"}],
			[{**_hours(), "end_time": "07:00:00"}],
			[{**_hours(), "start_time": "24:00:00"}],
			[{**_hours(), "workday": "__import__"}],
		]
		for value in invalid:
			with self.subTest(value=value), self.assertRaises(SLAContractError):
				build_sla_support_hours(value)


class SLAClockTests(unittest.TestCase):
	def test_elapsed_clock_and_fractional_pause_intervals(self) -> None:
		clock = SLAClock("Elapsed", {})
		start = datetime(2026, 10, 26, 8, 0, 0, 100000)
		end = datetime(2026, 10, 26, 8, 0, 0, 900000)
		self.assertAlmostEqual(clock.seconds(start, end), 0.8)
		self.assertEqual(clock.due(start, 0.8), end)

	def test_business_clock_skips_weekend_holiday_and_off_hours(self) -> None:
		clock = SLAClock(
			"Business Hours",
			{d: (8 * 3600, 17 * 3600) for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")},
			frozenset({"2026-10-26"}),
		)
		start = datetime(2026, 10, 23, 16)
		end = datetime(2026, 10, 27, 10)
		self.assertEqual(clock.seconds(start, end), 3 * 3600)
		self.assertEqual(clock.due(start, 3 * 3600), end)

	def test_business_clock_preserves_exact_closing_boundary(self) -> None:
		clock = SLAClock("Business Hours", {"Monday": (8 * 3600, 17 * 3600)})
		self.assertEqual(clock.due(datetime(2026, 10, 26, 16), 3600), datetime(2026, 10, 26, 17))
		self.assertEqual(clock.seconds(datetime(2026, 10, 26, 17), datetime(2026, 10, 26, 18)), 0)

	def test_insufficient_calendar_and_backwards_time_fail_boundedly(self) -> None:
		clock = SLAClock(
			"Business Hours", {"Monday": (28800, 61200)}, valid_from="2026-10-26", valid_to="2026-10-26"
		)
		with self.assertRaises(SLAContractError):
			clock.due(datetime(2026, 10, 26, 17), 1)
		with self.assertRaises(SLAContractError):
			clock.seconds(datetime(2026, 10, 27), datetime(2026, 10, 26))
		with self.assertRaises(SLAContractError):
			SLAClock("Business Hours", {}).due(datetime(2026, 10, 26), 1)


class SLAPolicyTests(unittest.TestCase):
	@staticmethod
	def payload() -> dict:
		return {
			"code": "SLA-TEST",
			"display_name": "Review SLA",
			"target_doctype": "Test Request",
			"company": "Test Company",
			"hospital": "TEST-HOSPITAL",
			"valid_from": "2026-01-01",
			"status_field": "status",
			"start_field": "submitted_at",
			"start_statuses_json": ["Pending"],
			"pause_statuses_json": [],
			"completion_statuses_json": ["Done"],
			"response_seconds": 60,
			"resolution_seconds": 120,
			"clock_basis": "Elapsed",
			"responsible_user": "Administrator",
			"escalation_steps_json": [{"level": 1, "delay_seconds": 0, "assigned_to": "Administrator"}],
		}

	def test_policy_canonicalization_is_stable(self):
		normalized = policy_payload(self.payload())
		self.assertEqual(normalized, policy_payload(normalized))
		self.assertEqual(
			sla_digest(normalized), sla_digest(policy_payload(dict(reversed(list(self.payload().items())))))
		)

	def test_policy_rejects_expressions_invalid_window_and_crossed_durations(self):
		for overrides in [
			{"condition": "doc.amount > 1"},
			{"valid_to": "2025-12-31"},
			{"resolution_seconds": 30},
			{"include_descendants": 1},
			{"responsible_user": "Guest"},
		]:
			with self.subTest(overrides=overrides), self.assertRaises(SLAContractError):
				policy_payload({**self.payload(), **overrides})

	def test_clock_choice_cannot_discard_a_supplied_calendar(self) -> None:
		self.assertEqual(normalize_sla_clock(clock_basis="Elapsed"), ("Elapsed", (), None))
		with self.assertRaises(SLAContractError):
			normalize_sla_clock(clock_basis="Elapsed", support_hours=[_hours()])
		with self.assertRaises(SLAContractError):
			normalize_sla_clock(clock_basis="Elapsed", holiday_list="2026 Holidays")
		with self.assertRaises(SLAContractError):
			normalize_sla_clock(clock_basis="Business Hours")
		basis, hours, holidays = normalize_sla_clock(
			clock_basis="Business Hours", support_hours=[_hours()], holiday_list="2026 Holidays"
		)
		self.assertEqual((basis, len(hours), holidays), ("Business Hours", 1, "2026 Holidays"))

	def test_start_pause_and_completion_states_are_unambiguous(self) -> None:
		self.assertEqual(
			validate_sla_status_groups(
				start_statuses=["Under Review"], pause_statuses=["On Hold"], completion_statuses=["Approved"]
			),
			(("Under Review",), ("On Hold",), ("Approved",)),
		)
		for paused in (["Under Review"], ["Approved"], ["On Hold", "On Hold"]):
			with self.subTest(paused=paused), self.assertRaises(SLAContractError):
				validate_sla_status_groups(
					start_statuses=["Under Review"], pause_statuses=paused, completion_statuses=["Approved"]
				)

	def test_durations_are_bounded_and_do_not_accept_boolean_or_fractional_values(self) -> None:
		self.assertEqual(normalize_sla_duration("3600", label="response"), 3600)
		for value in [True, 1.5, -1, 0, MAX_SLA_DURATION_SECONDS + 1]:
			with self.subTest(value=value), self.assertRaises(SLAContractError):
				normalize_sla_duration(value, label="response")

	def test_escalations_have_bounded_contiguous_levels_and_increasing_delays(self) -> None:
		steps = build_sla_escalation_steps(
			[
				{"level": 1, "delay_seconds": 0, "assigned_to": "manager@example.com"},
				{"level": 2, "delay_seconds": 3600, "assigned_to": "director@example.com"},
			]
		)
		self.assertEqual(steps[1].as_payload()["delay_seconds"], 3600)
		for values in [
			[],
			[{"level": 2, "delay_seconds": 0, "assigned_to": "manager@example.com"}],
			[{"level": 1, "delay_seconds": 0, "assigned_to": "Guest"}],
			[
				{"level": 1, "delay_seconds": 60, "assigned_to": "manager@example.com"},
				{"level": 2, "delay_seconds": 60, "assigned_to": "director@example.com"},
			],
		]:
			with self.subTest(values=values), self.assertRaises(SLAContractError):
				build_sla_escalation_steps(values)

	def test_only_declared_field_names_and_bounded_json_are_accepted(self) -> None:
		self.assertEqual(normalize_sla_field("submitted_at", label="start_field"), "submitted_at")
		for value in ["__class__", "doc.status", "status; drop table"]:
			with self.subTest(value=value), self.assertRaises(SLAContractError):
				normalize_sla_field(value, label="start_field")
		for value in ["[" + " " * 20000 + "]", '{"workday":"Monday"}', "not json"]:
			with self.subTest(value=value[:40]), self.assertRaises(SLAContractError):
				build_sla_support_hours(value)
