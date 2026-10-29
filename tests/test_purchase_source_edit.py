from __future__ import annotations

import unittest
from copy import deepcopy
from decimal import Decimal
from typing import cast

from medops.common.purchase_source_edit import validate_bound_purchase_order_edit
from medops.common.reservation_commitment import HISTORICAL_ORDER_STATUSES


def fixture():
	return {
		"name": "PO-BOUND",
		"company": "Company",
		"supplier": "Supplier",
		"currency": "CNY",
		"conversion_rate": "1.000000000",
		"transaction_date": "2026-10-26",
		"docstatus": 1,
		"status": "To Receive and Bill",
		"items": [
			{
				"name": name,
				"parent": "PO-BOUND",
				"parenttype": "Purchase Order",
				"parentfield": "items",
				"docstatus": 1,
				"material_request": "MR-BOUND",
				"material_request_item": "MR-LINE",
				"item_code": "Item",
				"uom": "Unit",
				"cost_center": "Center",
				"expense_account": "Expense",
				"project": None,
				"qty": "1.000000000",
				"rate": "10.000000000",
				"amount": "10.000000000",
				"base_amount": "10.000000000",
				"is_free_item": 0,
			}
			for name in ("BOUND", "FREE")
		],
	}


class TestPurchaseSourceEdit(unittest.TestCase):
	def test_native_progress_and_unbound_changes_do_not_rewrite_binding(self):
		for status in HISTORICAL_ORDER_STATUSES:
			before, after = fixture(), fixture()
			after.update(status=status, per_received=100, per_billed=100, remarks="native progress")
			after["items"][0].update(qty=1.0, rate=Decimal(10), received_qty=1, billed_amt=10, project="")
			after["items"][1].update(qty=3, rate=30, amount=90)
			with self.subTest(status=status):
				validate_bound_purchase_order_edit(before, after, ("BOUND",))
		validate_bound_purchase_order_edit({}, {}, ())
		for status in HISTORICAL_ORDER_STATUSES:
			before, after = fixture(), fixture()
			before["status"] = status
			with self.subTest(reopen=status):
				validate_bound_purchase_order_edit(before, after, ("BOUND",))

	def test_all_header_commercial_facts_are_protected(self):
		for field, value in (
			("name", "PO-NEW"),
			("company", "Other"),
			("supplier", "Other"),
			("supplier", "Supplier "),
			("currency", "USD"),
			("conversion_rate", 2),
			("transaction_date", "2026-10-27"),
		):
			after = fixture()
			after[field] = value
			with self.subTest(field=field), self.assertRaises(ValueError):
				validate_bound_purchase_order_edit(fixture(), after, ("BOUND",))

	def test_bound_item_commercial_and_link_facts_are_protected(self):
		for field, value in (
			("material_request", "Other"),
			("material_request_item", "Other"),
			("item_code", "Other"),
			("uom", "Other"),
			("cost_center", "Other"),
			("expense_account", "Other"),
			("project", "Other"),
			("qty", 2),
			("rate", 11),
			("amount", 11),
			("base_amount", 11),
			("is_free_item", 1),
		):
			after = fixture()
			after["items"][0][field] = value
			with self.subTest(field=field), self.assertRaises(ValueError):
				validate_bound_purchase_order_edit(fixture(), after, ("BOUND",))

	def test_bound_children_cannot_move_or_disappear_or_duplicate(self):
		for mode in ("remove", "replace", "duplicate", "parent", "parenttype", "parentfield"):
			after = fixture()
			if mode == "remove":
				after["items"].pop(0)
			elif mode == "replace":
				after["items"][0]["name"] = "NEW-LINE"
			elif mode == "duplicate":
				after["items"].append(deepcopy(after["items"][0]))
			else:
				after["items"][0][mode] = "Other"
			with self.subTest(mode=mode), self.assertRaises(ValueError):
				validate_bound_purchase_order_edit(fixture(), after, ("BOUND",))

	def test_float_loss_nonfinite_and_excess_precision_are_refused(self):
		before = fixture()
		before["items"][0]["amount"] = "999999999999.000000001"
		after = deepcopy(before)
		after["items"][0]["amount"] = float(before["items"][0]["amount"])
		with self.assertRaises(ValueError):
			validate_bound_purchase_order_edit(before, after, ("BOUND",))
		for value in (True, float("nan"), float("inf"), "0.0000000001"):
			after = fixture()
			after["items"][0]["rate"] = value
			with self.subTest(value=str(value)), self.assertRaises(ValueError):
				validate_bound_purchase_order_edit(fixture(), after, ("BOUND",))

	def test_draft_cancel_unknown_state_and_child_docstatus_are_refused(self):
		for status in ("Draft", "Cancelled", "Unknown", None):
			after = fixture()
			after["status"] = status
			with self.subTest(status=status), self.assertRaises(ValueError):
				validate_bound_purchase_order_edit(fixture(), after, ("BOUND",))
		for value in (0, 2, 1.0, True, None):
			for target in ("parent", "child"):
				after = fixture()
				(after if target == "parent" else after["items"][0])["docstatus"] = value
				with self.subTest(target=target, value=value), self.assertRaises(ValueError):
					validate_bound_purchase_order_edit(fixture(), after, ("BOUND",))

	def test_malformed_existing_binding_is_not_treated_as_unbound(self):
		for lines in (("MISSING",), ("BOUND", "BOUND"), (" BOUND",), ["BOUND"]):
			with self.subTest(lines=lines), self.assertRaises(ValueError):
				validate_bound_purchase_order_edit(fixture(), fixture(), cast(tuple[str, ...], lines))
		before = fixture()
		before["conversion_rate"] = 2
		with self.assertRaises(ValueError):
			validate_bound_purchase_order_edit(before, deepcopy(before), ("BOUND",))
