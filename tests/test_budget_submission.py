from __future__ import annotations

import unittest
from copy import deepcopy
from typing import Any

from medops.common.budget_approval import (
	build_department_submission,
	department_snapshot_from_payload,
	freeze_department_facts,
)


def source(precision=9) -> tuple[dict[str, Any], dict[str, Any]]:
	context = dict(
		company="Company",
		hospital="Hospital",
		organization_version="Org",
		fiscal_year="FY",
		currency="CNY",
		currency_precision=precision,
	)
	version = dict(
		context,
		name="Version",
		docstatus=1,
		status="Frozen",
		period_start="2026-01-01",
		period_end="2026-12-31",
	)
	amount = "999999999999.000000001" if precision == 9 else "1" if precision == 0 else "1." + "0" * precision
	plan = dict(
		context,
		name="Plan",
		budget_version="Version",
		organization_unit="Unit",
		cost_center="Center",
		subject="预算",
		remarks=None,
		revision=3,
		docstatus=0,
		status="Draft",
		total_amount=amount,
		lines=[
			dict(
				budget_subject="Subject",
				funding_source="Fund",
				account="Account",
				project=None,
				period_index=1,
				period_start="2026-01-01",
				period_end="2026-01-31",
				amount=amount,
				description=None,
			)
		],
	)
	return plan, version


def snapshot(plan=None, version=None):
	default_plan, default_version = source()
	return freeze_department_facts(
		plan or default_plan,
		version or default_version,
		version_digest="a" * 64,
		applicant="user@example.com",
		posting_date="2026-10-26",
	)


class TestBudgetSubmissionFacts(unittest.TestCase):
	def test_nine_digit_money_and_original_grain_round_trip(self):
		item = snapshot()
		self.assertEqual(item.total_amount, "999999999999.000000001")
		self.assertEqual(item.lines[0].organization_unit, "Unit")
		self.assertEqual(item.lines[0].funding_source, "Fund")
		self.assertEqual(item.lines[0].origin_plan, "Plan")
		self.assertEqual(department_snapshot_from_payload(item.as_dict()), item)

	def test_all_supported_captured_precisions_conserve_money(self):
		for precision in range(10):
			with self.subTest(precision=precision):
				plan, version = source(precision)
				item = snapshot(plan, version)
				self.assertEqual(item.total_amount, plan["total_amount"])

	def test_draft_withdrawn_and_foreign_context_are_rejected(self):
		plan, version = source()
		for key, value in (
			("status", "Draft"),
			("docstatus", 0),
			("currency_precision", 2),
			("company", "Foreign"),
		):
			with self.subTest(key=key), self.assertRaises(ValueError):
				snapshot(plan, {**version, key: value})
		with self.assertRaises(ValueError):
			snapshot({**plan, "status": "Withdrawn"}, version)

	def test_duplicate_and_forged_origin_grains_are_rejected(self):
		facts = snapshot().as_dict()
		with self.assertRaises(ValueError):
			department_snapshot_from_payload({**facts, "lines": facts["lines"] * 2})
		for field, value in (
			("origin_plan", "Other"),
			("organization_unit", "Other"),
			("funding_source", "Other"),
			("fact_key", "b" * 64),
			("origin_version_digest", "b" * 64),
		):
			bad = deepcopy(facts)
			bad["lines"][0][field] = value
			with self.subTest(field=field), self.assertRaises(ValueError):
				department_snapshot_from_payload(bad)

	def test_reordered_stored_rows_have_the_same_canonical_digest(self):
		plan, version = source()
		plan["lines"].append(
			{
				**plan["lines"][0],
				"period_index": 2,
				"period_start": "2026-02-01",
				"period_end": "2026-02-28",
				"amount": "0.000000001",
			}
		)
		plan["total_amount"] = "999999999999.000000002"
		item = snapshot(plan, version)
		facts = item.as_dict()
		facts["lines"].reverse()
		self.assertEqual(department_snapshot_from_payload(facts).snapshot_digest, item.snapshot_digest)

	def test_amount_and_digest_tampering_are_rejected(self):
		facts = snapshot().as_dict()
		for field, value in (
			("total_amount", "0.000000000"),
			("snapshot_digest", "b" * 64),
			("identity_key", "b" * 64),
			("source_plan_revision", 0),
		):
			with self.subTest(field=field), self.assertRaises(ValueError):
				department_snapshot_from_payload({**facts, field: value})
		bad = deepcopy(facts)
		bad["lines"][0]["amount"] = 1.01
		with self.assertRaises(ValueError):
			department_snapshot_from_payload(bad)

	def test_fiscal_calendar_escape_and_empty_facts_are_rejected(self):
		plan, version = source()
		plan["lines"][0]["period_end"] = "2027-01-01"
		with self.assertRaises(ValueError):
			snapshot(plan, version)
		plan["lines"] = []
		with self.assertRaises(ValueError):
			snapshot(plan, version)

	def test_command_accepts_only_the_original_plan_and_revision(self):
		command = build_department_submission({"source_plan": "Plan", "expected_revision": 3})
		self.assertEqual(command.expected_revision, 3)
		for data in (
			{"source_plan": "Plan", "expected_revision": True},
			{"source_plan": "Plan", "expected_revision": 3, "total_amount": "1"},
			{"source_plan": "Plan", "expected_revision": 0},
		):
			with self.subTest(data=data), self.assertRaises(ValueError):
				build_department_submission(data)
