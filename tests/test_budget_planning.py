from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any

from medops.common.budget_planning import (
	BudgetPlanningError,
	allocate_amount,
	amount_total,
	build_action,
	build_lines,
	build_master,
	build_plan,
	build_version,
	decimal_amount,
	master_identity,
	plan_definition,
	planning_digest,
	version_definition,
)
from medops.common.domain_service import canonical_json


def line(**changes):
	return {
		"budget_subject": "SUBJECT",
		"funding_source": "FUND",
		"account": "ACCOUNT",
		"period_index": 1,
		"amount": "0.10",
		**changes,
	}


class BudgetPlanningContractTest(unittest.TestCase):
	def test_exact_high_precision_amounts_and_totals(self):
		value = "999999999999.123456789"
		self.assertEqual(decimal_amount(value, 9), value)
		self.assertEqual(amount_total(["0.1", "0.2"], 2), "0.30")
		self.assertEqual(decimal_amount("1.23000", 2), "1.23")
		self.assertEqual(decimal_amount("0", 0), "0")

	def test_non_finite_negative_overflow_and_overprecision_are_rejected(self):
		for value in (True, None, "NaN", "Infinity", "-0.01", "1000000000000", "0.0000000001", "1,000.00"):
			with self.assertRaises(ValueError):
				decimal_amount(value)
		with self.assertRaises(BudgetPlanningError):
			decimal_amount("1.001", 2)
		with self.assertRaises(BudgetPlanningError):
			amount_total(["999999999999.99", "0.02"], 2)

	def test_allocation_conserves_every_minor_unit_including_zero_and_remainders(self):
		for value, count, precision in (("100.00", 12, 2), ("1", 12, 0), ("0.000000001", 12, 9), ("0", 1, 2)):
			rows = allocate_amount(value, count, precision)
			self.assertEqual(sum(map(Decimal, rows)), Decimal(value))
			self.assertLessEqual(
				max(map(Decimal, rows)) - min(map(Decimal, rows)), Decimal(1).scaleb(-precision)
			)
		self.assertEqual(allocate_amount("0.02", 3, 2), ["0.01", "0.01", "0.00"])

	def test_master_identity_separates_hospitals_and_business_dimensions(self):
		payload = {
			"company": "C",
			"hospital": "H",
			"code": "BUDGET",
			"display_name": "预算科目",
			"purpose": "Budget Subject",
			"valid_from": "2026-01-01",
		}
		one = build_master("dimension", payload)
		two = build_master("dimension", {**payload, "hospital": "OTHER"})
		self.assertNotEqual(master_identity(one), master_identity(two))
		with self.assertRaises(ValueError):
			build_master("dimension", {**payload, "account": "wrong equivalent"})

	def test_versions_require_real_reference_names_and_optimistic_revisions(self):
		payload = {
			"company": "C",
			"hospital": "H",
			"organization_version": "O",
			"fiscal_year": "FY",
			"subject": "年度预算",
		}
		self.assertEqual(build_version(payload).distribution_frequency, "Monthly")
		for change in (
			{"name": "OLD"},
			{"expected_revision": 1},
			{"distribution_frequency": "Weekly"},
			{"currency": "CNY"},
		):
			with self.assertRaises(ValueError):
				build_version({**payload, **change})

	def test_lines_preserve_all_budget_grain_dimensions_and_reject_duplicates(self):
		self.assertEqual(len(build_lines([line(), line(funding_source="OTHER")])), 2)
		for values in ([line(), line(amount="2")], [line(period_index=0)], [line(sql="ignored")], []):
			with self.assertRaises(ValueError):
				build_lines(values)

	def test_plan_request_hash_is_order_independent_and_does_not_weaken_shared_limits(self):
		base = {"budget_version": "V", "organization_unit": "U", "subject": "编制计划"}
		rows = [line(period_index=i + 1) for i in range(100)]
		one = build_plan({**base, "lines": rows}).as_request_payload()
		two = build_plan({**base, "lines": list(reversed(rows))}).as_request_payload()
		self.assertEqual(one, two)
		self.assertLess(len(canonical_json(one)), 500)
		self.assertEqual([row.period_index for row in build_lines(list(reversed(rows)))], list(range(1, 101)))
		self.assertNotEqual(
			one["lines_digest"],
			build_plan(
				{**base, "lines": [*rows[:-1], line(period_index=100, amount="1")]}
			).as_request_payload()["lines_digest"],
		)

	def test_actions_cannot_carry_unreviewed_approval_or_identity(self):
		self.assertEqual(build_action({"name": "V", "expected_revision": 1}).name, "V")
		for key in ("approved_amount", "docstatus", "user", "approval"):
			with self.assertRaises(ValueError):
				build_action({"name": "V", "expected_revision": 1, key: True})

	def test_digest_rejects_noncanonical_financial_values(self):
		with self.assertRaises(ValueError):
			planning_digest({"amount": float("nan")})

	def test_sealed_financial_digest_survives_workflow_but_detects_amount_change(self):
		version = {
			key: None
			for key in (
				"name",
				"company",
				"hospital",
				"organization_version",
				"fiscal_year",
				"version_number",
				"subject",
				"currency",
				"currency_precision",
				"period_start",
				"period_end",
				"distribution_frequency",
				"cloned_from",
				"remarks",
			)
		}
		plan: dict[str, Any] = {
			key: None
			for key in (
				"name",
				"company",
				"hospital",
				"budget_version",
				"organization_version",
				"organization_unit",
				"cost_center",
				"fiscal_year",
				"currency",
				"currency_precision",
				"subject",
				"remarks",
				"total_amount",
				"lines",
			)
		}
		plan.update(total_amount="0.10", lines=[line()])
		self.assertEqual(
			version_definition(version),
			version_definition({**version, "status": "Frozen", "revision": 2, "docstatus": 1}),
		)
		self.assertEqual(
			planning_digest(plan_definition(plan)),
			planning_digest(plan_definition({**plan, "status": "Approved", "revision": 4, "docstatus": 1})),
		)
		self.assertNotEqual(
			planning_digest(plan_definition(plan)),
			planning_digest(plan_definition({**plan, "total_amount": "0.11"})),
		)
