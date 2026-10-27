from __future__ import annotations

import unittest
from dataclasses import replace

from medops.common.budget_control import budget_control_key
from medops.common.budget_control_history import ReleaseInventoryEntry
from medops.common.budget_reservation_events import ReservationPosition
from medops.common.budget_scope_observation import observe_budget_scope
from tests.test_budget_control import key


class BudgetScopeObservationTest(unittest.TestCase):
	def setUp(self):
		self.key = budget_control_key(key())
		self.other = budget_control_key(key(budget_subject="Other Subject"))
		self.position = ReservationPosition(
			"reservation",
			self.key,
			"CNY",
			2,
			3,
			"20.00",
			"12.00",
			"5.00",
			"17.00",
			"8.00",
			"7.00",
		)

	def entry(self, name, amount, budget_key=None, currency="CNY", precision=2):
		return ReleaseInventoryEntry(
			name, budget_key or self.key, currency, precision, amount, "Account", "Center"
		)

	def test_subtracts_open_states_once_and_nets_reversals(self):
		result = observe_budget_scope(
			self.key,
			"CNY",
			2,
			[self.entry("allocation", "100.00"), self.entry("reversal", "-30.00")],
			[self.position],
		)
		self.assertEqual(
			(result.net_releases, result.exposure, result.difference), ("70.00", "20.00", "50.00")
		)

	def test_ignores_other_budget_keys_without_cross_contamination(self):
		other = replace(self.position, reservation="other", key=self.other, open_reservation="900.00")
		result = observe_budget_scope(
			self.key,
			"CNY",
			2,
			[self.entry("selected", "50.00"), self.entry("other", "900.00", self.other)],
			[self.position, other],
		)
		self.assertEqual(
			(result.net_releases, result.exposure, result.difference), ("50.00", "20.00", "30.00")
		)

	def test_negative_difference_remains_visible(self):
		result = observe_budget_scope(self.key, "CNY", 2, [self.entry("small", "1.00")], [self.position])
		self.assertEqual(result.difference, "-19.00")

	def test_duplicate_records_and_incompatible_currency_or_precision_fail(self):
		entry = self.entry("duplicate", "1.00")
		for history, usage in (
			([entry, entry], []),
			([], [self.position, self.position]),
			([self.entry("usd", "1.00", currency="USD")], []),
			([], [replace(self.position, precision=3)]),
		):
			with self.subTest(history=history, usage=usage), self.assertRaises(ValueError):
				observe_budget_scope(self.key, "CNY", 2, history, usage)

	def test_rejects_unrepresentable_usage_and_overflow(self):
		bad = replace(self.position, open_reservation="0.001")
		large = self.entry("large", "999999999999999.00")
		for history, usage in (([], [bad]), ([large, self.entry("more", "1.00")], [])):
			with self.subTest(history=history, usage=usage), self.assertRaises(ValueError):
				observe_budget_scope(self.key, "CNY", 2, history, usage)


if __name__ == "__main__":
	unittest.main()
