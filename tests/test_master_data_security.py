from __future__ import annotations

import json
import unittest

from medops.common.master_data import MASTER_DATA_TARGET_POLICIES
from medops.common.master_data_security import evaluation_fields, merge_evaluation_fields


class EvaluationPrivacyTests(unittest.TestCase):
	def test_new_evaluation_tracks_only_its_reviewed_field(self):
		self.assertEqual(json.loads(merge_evaluation_fields(None, "Item", "item_name")), ["item_name"])

	def test_changed_rule_keeps_all_prior_field_requirements(self):
		self.assertEqual(
			json.loads(merge_evaluation_fields('["item_name"]', "Item", "stock_uom")),
			["item_name", "stock_uom"],
		)

	def test_unknown_legacy_history_uses_a_conservative_superset(self):
		result = merge_evaluation_fields(None, "Item", "item_name", legacy=True)
		self.assertEqual(
			set(evaluation_fields(result, "Item")), set(MASTER_DATA_TARGET_POLICIES["Item"].fields_by_name)
		)

	def test_access_contract_rejects_empty_unknown_duplicate_and_oversize_fields(self):
		for value in (None, [], ["password"], ["item_name", "item_name"], " " * 4097):
			with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
				evaluation_fields(value, "Item")
