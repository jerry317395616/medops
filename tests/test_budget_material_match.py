from __future__ import annotations

import unittest
from typing import Any

from medops.common.budget_control import budget_control_key
from medops.common.budget_control_history import ReleaseInventoryEntry
from medops.common.budget_material_match import match_purchase_release_keys
from tests.test_budget_control import key


class PurchaseReleaseMatchTest(unittest.TestCase):
	def setUp(self):
		self.key = budget_control_key(key())
		self.source: dict[str, Any] = dict(
			company="Hospital Co",
			hospital="Hospital",
			organization_unit="Department A",
			project=None,
			account="Expense Account",
			cost_center="Cost Center",
			currency="CNY",
			budget_date="2026-01-15",
			amount="1.000000000",
		)

	def entry(
		self,
		name,
		amount,
		*,
		budget_key=None,
		currency="CNY",
		precision=2,
		account="Expense Account",
		center="Cost Center",
	):
		return ReleaseInventoryEntry(
			name, budget_key or self.key, currency, precision, amount, account, center
		)

	def test_positive_net_matching_release_produces_candidate_without_balance(self):
		entries = [
			self.entry("release", "100.00"),
			self.entry("reverse", "-30.00"),
			self.entry("other-account", "900.00", account="Other Account"),
			self.entry("other-center", "800.00", center="Other Center"),
		]
		result = match_purchase_release_keys([self.key], entries, **self.source)
		self.assertEqual(len(result), 1)
		self.assertEqual((result[0].key, result[0].currency, result[0].precision), (self.key, "CNY", 2))
		self.assertFalse(hasattr(result[0], "available"))

	def test_fully_reversed_or_unrelated_release_does_not_match(self):
		for entries in (
			[],
			[self.entry("release", "100.00"), self.entry("reverse", "-100.00")],
			[self.entry("other", "900.00", account="Other Account")],
		):
			with self.subTest(entries=entries):
				self.assertEqual(match_purchase_release_keys([self.key], entries, **self.source), ())

	def test_source_context_and_budget_date_are_not_guessed(self):
		for changes in (
			{"company": "Another Company"},
			{"organization_unit": "Another Unit"},
			{"project": "Another Project"},
			{"budget_date": "2026-02-01"},
			{"account": ""},
			{"amount": "0.000000000"},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				match_purchase_release_keys([self.key], [], **(self.source | changes))

	def test_mixed_currency_precision_and_unrepresentable_source_fail_closed(self):
		for entries, changes in (
			([self.entry("usd", "1.00", currency="USD")], {}),
			([self.entry("a", "1.00"), self.entry("b", "1.000", precision=3)], {}),
			([self.entry("a", "1.00")], {"amount": "1.001000000"}),
		):
			with self.subTest(entries=entries, changes=changes), self.assertRaises(ValueError):
				match_purchase_release_keys([self.key], entries, **(self.source | changes))

	def test_duplicates_outside_scope_and_overlapping_periods_fail_closed(self):
		entry = self.entry("one", "1.00")
		outside = self.entry(
			"outside", "1.00", budget_key=budget_control_key(key(organization_unit="Elsewhere"))
		)
		overlap = self.entry(
			"overlap",
			"1.00",
			budget_key=budget_control_key(key(period_start="2026-01-15", period_end="2026-02-15")),
		)
		for entries in ([entry, entry], [outside], [overlap]):
			with self.subTest(entries=entries), self.assertRaises(ValueError):
				match_purchase_release_keys([self.key], entries, **self.source)


if __name__ == "__main__":
	unittest.main()
