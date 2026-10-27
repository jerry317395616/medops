from __future__ import annotations

import unittest
from decimal import Decimal, localcontext
from unittest.mock import PropertyMock, patch

from medops.common.budget_control import (
	BudgetControlKey,
	available_budget,
	budget_control_key,
	fits_available_budget,
	ordered_budget_keys,
)
from medops.common.exact_money import ExactMoneyError


def key(**changes):
	return {
		"company": "Hospital Co",
		"hospital": "Hospital",
		"organization_unit": "Department A",
		"fiscal_year": "FY-2026",
		"budget_subject": "Equipment",
		"funding_source": "Own Funds",
		"project": None,
		"period_index": 1,
		**changes,
	}


def components(**changes):
	return {
		"approved": "1000",
		"adjustment_in": "20",
		"adjustment_out": "30",
		"reserved": "400",
		"committed": "200",
		"actual": "100",
		"released": "50",
		**changes,
	}


class BudgetControlContractTest(unittest.TestCase):
	def test_full_budget_key_is_canonical_and_each_dimension_matters(self):
		first = budget_control_key(key(period_index="1", project=""))
		self.assertEqual(first, budget_control_key(key()))
		self.assertEqual(first.identity, budget_control_key(key()).identity)
		for field, value in (
			("company", "Other Company"),
			("hospital", "Other Hospital"),
			("organization_unit", "Department B"),
			("fiscal_year", "FY-2027"),
			("budget_subject", "Drugs"),
			("funding_source", "Grant"),
			("project", "Project 1"),
			("period_index", 2),
		):
			with self.subTest(field=field):
				self.assertNotEqual(first.identity, budget_control_key(key(**{field: value})).identity)

	def test_key_rejects_missing_extra_invalid_and_noncanonical_direct_values(self):
		missing = key()
		del missing["project"]
		for invalid in (
			missing,
			key(extra="x"),
			key(period_index=True),
			key(project="a\nb"),
			key(project=" "),
		):
			with self.subTest(invalid=invalid), self.assertRaises(ValueError):
				budget_control_key(invalid)
		with self.assertRaises(ValueError):
			BudgetControlKey(**key(period_index=True))
		with self.assertRaises(ValueError):
			BudgetControlKey(**key(company=" Hospital Co "))

	def test_multi_key_guard_order_is_deterministic_and_duplicate_free(self):
		first = budget_control_key(key())
		second = budget_control_key(key(organization_unit="Department B"))
		third = budget_control_key(key(period_index=2))
		one = ordered_budget_keys([first, second, third, first])
		two = ordered_budget_keys([third, second, first])
		self.assertEqual(one, two)
		self.assertEqual(len(one), 3)
		self.assertEqual([item.identity for item in one], sorted(item.identity for item in one))

	def test_guard_key_input_is_bounded_and_rejects_noncanonical_types(self):
		valid = budget_control_key(key())
		for invalid in ([], [valid, key()], [valid] * 1001, iter([valid])):
			with self.subTest(value_type=type(invalid).__name__), self.assertRaises(ValueError):
				ordered_budget_keys(invalid)

	def test_guard_key_hash_collision_fails_closed(self):
		first = budget_control_key(key())
		second = budget_control_key(key(organization_unit="Department B"))
		with patch.object(BudgetControlKey, "identity", new_callable=PropertyMock, return_value="0" * 64):
			with self.assertRaisesRegex(ValueError, "identity collision"):
				ordered_budget_keys([first, second])

	def test_lexical_aliases_are_not_mistaken_for_database_identity_resolution(self):
		original = budget_control_key(key(organization_unit="Department A"))
		alias = budget_control_key(key(organization_unit="department a"))
		self.assertNotEqual(original.identity, alias.identity)
		# A future service must resolve both to the actual persisted document name
		# before acquiring any guard; this pure function cannot determine collation.

	def test_formula_uses_complete_components_and_does_not_mutate_input(self):
		values = components()
		before = dict(values)
		self.assertEqual(available_budget(values, 2), "340.00")
		self.assertEqual(values, before)
		incomplete = components()
		del incomplete["released"]
		for invalid in (incomplete, components(unexpected="0")):
			with self.subTest(invalid=invalid), self.assertRaises(ValueError):
				available_budget(invalid, 2)

	def test_exact_fractional_values_ignore_global_decimal_context(self):
		values = components(
			approved="999999999999999.999999999",
			adjustment_in="0.000000001",
			adjustment_out="0.000000001",
			reserved="0.000000001",
			committed="0",
			actual="0",
			released="0",
		)
		with localcontext() as context:
			context.prec = 3
			self.assertEqual(available_budget(values, 9), "999999999999999.999999998")
			self.assertTrue(fits_available_budget("0.000000001", "0.000000001", 9))
			self.assertFalse(fits_available_budget("0.000000001", "0.000000002", 9))

	def test_negative_balance_remains_visible_but_cannot_cover_positive_amount(self):
		value = available_budget(components(approved="0", adjustment_in="0", released="0"), 0)
		self.assertEqual(value, "-730")
		self.assertFalse(fits_available_budget(value, "1", 0))
		with self.assertRaises(ValueError):
			fits_available_budget("0", "0", 0)

	def test_invalid_precision_float_negative_and_result_overflow_are_rejected(self):
		for values, precision in (
			(components(approved=1.2), 2),
			(components(approved=True), 2),
			(components(actual="-1"), 2),
			(components(approved="1.001"), 2),
			(components(), True),
			(components(approved=Decimal("NaN")), 2),
			(components(approved="999999999999999.999999999", adjustment_in="999999999999999.999999999"), 9),
		):
			with self.subTest(values=values, precision=precision), self.assertRaises(ExactMoneyError):
				available_budget(values, precision)


if __name__ == "__main__":
	unittest.main()
