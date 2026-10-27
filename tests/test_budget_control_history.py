from __future__ import annotations

import unittest

from medops.common.budget_control import budget_control_key
from medops.common.budget_control_history import ReleaseInventoryEntry, require_compatible_release_history
from tests.test_budget_control import key


class ReleaseHistoryCompatibilityTest(unittest.TestCase):
	def setUp(self):
		self.current = budget_control_key(key())
		self.entry = ReleaseInventoryEntry("entry-1", self.current, "CNY", 2, "100.00")

	def test_matching_and_nonoverlapping_periods_are_compatible(self):
		later = budget_control_key(key(period_start="2026-02-01", period_end="2026-02-28"))
		other = ReleaseInventoryEntry("entry-2", later, "USD", 3, "-10.000")
		require_compatible_release_history([self.current], [self.entry, other], currency="CNY", precision=2)

	def test_different_currencies_or_precisions_at_same_key_are_rejected(self):
		for currency, precision in (("USD", 2), ("CNY", 3)):
			with self.subTest(currency=currency, precision=precision), self.assertRaises(ValueError):
				require_compatible_release_history(
					[self.current], [self.entry], currency=currency, precision=precision
				)

	def test_overlapping_and_cross_year_periods_are_rejected(self):
		for changes in (
			{"period_start": "2026-01-15", "period_end": "2026-02-15"},
			{"fiscal_year": "Other Year"},
		):
			other = ReleaseInventoryEntry("entry-2", budget_control_key(key(**changes)), "CNY", 2, "1.00")
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				require_compatible_release_history([self.current], [other], currency="CNY", precision=2)

	def test_duplicate_or_outside_scope_history_is_rejected(self):
		outside = ReleaseInventoryEntry(
			"entry-2", budget_control_key(key(organization_unit="Another Unit")), "CNY", 2, "1.00"
		)
		for history in ([self.entry, self.entry], [outside], [self.entry] * 50001):
			with self.subTest(count=len(history)), self.assertRaises(ValueError):
				require_compatible_release_history([self.current], history, currency="CNY", precision=2)

	def test_invalid_amount_currency_and_precision_are_rejected(self):
		for currency, precision, amount in (("CNY", 2, "1"), (" CNY ", 2, "1.00"), ("CNY", True, "1")):
			with self.subTest(currency=currency, precision=precision), self.assertRaises(ValueError):
				ReleaseInventoryEntry("entry", self.current, currency, precision, amount)
		with self.assertRaises(ValueError):
			require_compatible_release_history([self.current], [], currency=" CNY ", precision=2)


if __name__ == "__main__":
	unittest.main()
