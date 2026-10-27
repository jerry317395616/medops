from __future__ import annotations

import unittest
from dataclasses import replace

from medops.common.budget_control import KEY_FIELDS, budget_control_key, ordered_budget_keys
from medops.common.budget_control_release import budget_keys_for_release_batch
from medops.common.budget_ledger import allocation_values
from tests.test_budget_ledger import approved_release, reverse


class BudgetControlReleaseTest(unittest.TestCase):
	def test_release_keys_match_each_original_allocation_grain(self):
		first = approved_release()
		rows = list(allocation_values(first, posting_time="08:00:00"))
		expected = ordered_budget_keys(
			[budget_control_key({field: row[field] for field in KEY_FIELDS}) for row in rows]
		)
		actual = budget_keys_for_release_batch(first)
		self.assertEqual(actual, expected)
		self.assertEqual(
			{item.organization_unit for item in actual}, {row["organization_unit"] for row in rows}
		)

	def test_full_reversal_uses_the_same_original_keys(self):
		first = approved_release()
		self.assertEqual(
			budget_keys_for_release_batch(first),
			budget_keys_for_release_batch(reverse(first), original=first),
		)

	def test_changed_or_missing_source_fails_closed(self):
		first = approved_release()
		for bad, original in (
			(reverse(first), None),
			(replace(reverse(first), reversal_of="other"), first),
			(replace(first, total_amount="1"), None),
			(first, first),
		):
			with self.subTest(operation=bad.operation), self.assertRaises(ValueError):
				budget_keys_for_release_batch(bad, original=original)

	def test_oversized_batch_is_rejected_without_truncation(self):
		first = approved_release()
		snapshot = replace(first.snapshot, lines=first.snapshot.lines * 1001)
		with self.assertRaisesRegex(ValueError, "exceeds key limit"):
			budget_keys_for_release_batch(replace(first, snapshot=snapshot))
		with self.assertRaisesRegex(ValueError, "exceeds key limit"):
			budget_keys_for_release_batch(reverse(first), original=replace(first, snapshot=snapshot))
