from __future__ import annotations

import unittest
from copy import deepcopy
from decimal import Decimal

from medops.common.material_source_edit import validate_bound_material_request_edit


def fixture():
	return {
		"name": "MR-BOUND",
		"company": "Company",
		"material_request_type": "Purchase",
		"transaction_date": "2026-10-26",
		"docstatus": 1,
		"status": "Pending",
		"items": [
			{
				"name": name,
				"item_code": "Item",
				"uom": "Unit",
				"cost_center": "Center",
				"expense_account": "Expense",
				"project": None,
				"qty": "1.000000000",
				"rate": "10.000000000",
				"amount": "10.000000000",
			}
			for name in ("BOUND", "FREE")
		],
	}


class TestMaterialSourceEdit(unittest.TestCase):
	def test_progress_and_unbound_lines_remain_native_operations(self):
		before, after = fixture(), fixture()
		after.update(status="Ordered", modified="a later native timestamp", remarks="operational note")
		after["items"][0].update(qty=1.0, rate=Decimal("10"), amount=10)
		after["items"][1].update(qty=2, rate=30, amount=60)
		validate_bound_material_request_edit(before, after, ("BOUND",))
		validate_bound_material_request_edit(before, {}, ())

	def test_bound_economic_and_header_fields_cannot_change(self):
		for field, value in (
			("company", "Other"),
			("name", "MR-NEW"),
			("transaction_date", "2026-10-27"),
			("material_request_type", "Material Transfer"),
		):
			with self.subTest(field=field), self.assertRaises(ValueError):
				after = fixture()
				after[field] = value
				validate_bound_material_request_edit(fixture(), after, ("BOUND",))
		for field, value in (
			("item_code", "Other"),
			("uom", "Other"),
			("cost_center", "Other"),
			("expense_account", "Other"),
			("project", "Other"),
			("qty", 2),
			("rate", 11),
			("amount", 11),
		):
			with self.subTest(field=field), self.assertRaises(ValueError):
				after = fixture()
				after["items"][0][field] = value
				validate_bound_material_request_edit(fixture(), after, ("BOUND",))

	def test_bound_identity_cannot_be_removed_duplicated_or_replaced(self):
		for mode in ("remove", "duplicate", "replace"):
			after = fixture()
			if mode == "remove":
				after["items"].pop(0)
			elif mode == "duplicate":
				after["items"].append(deepcopy(after["items"][0]))
			else:
				after["items"][0]["name"] = "NEW-LINE"
			with self.subTest(mode=mode), self.assertRaises(ValueError):
				validate_bound_material_request_edit(fixture(), after, ("BOUND",))

	def test_float_comparison_cannot_hide_lost_decimal_digits_or_nonfinite_values(self):
		before = fixture()
		before["items"][0]["amount"] = "999999999999.000000001"
		after = deepcopy(before)
		after["items"][0]["amount"] = float(before["items"][0]["amount"])
		with self.assertRaises(ValueError):
			validate_bound_material_request_edit(before, after, ("BOUND",))
		for value in (True, float("nan"), float("inf"), "0.0000000001"):
			after = fixture()
			after["items"][0]["rate"] = value
			with self.subTest(value=str(value)), self.assertRaises(ValueError):
				validate_bound_material_request_edit(fixture(), after, ("BOUND",))

	def test_terminal_and_uninspectable_native_states_require_another_protocol(self):
		for state in ("Stopped", "Cancelled", "Draft", "Unknown"):
			after = fixture()
			after["status"] = state
			with self.subTest(state=state), self.assertRaises(ValueError):
				validate_bound_material_request_edit(fixture(), after, ("BOUND",))
		for status in (0, 2):
			after = fixture()
			after["docstatus"] = status
			with self.subTest(docstatus=status), self.assertRaises(ValueError):
				validate_bound_material_request_edit(fixture(), after, ("BOUND",))
