"""Native ERPNext source facts and exact MariaDB decimal reads."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services import material_request_source
from medops.tests import test_budget_consolidation as source_fixture


class TestBudgetMaterialRequestSource(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.organization, cls.units, _, _ = source_fixture.fixtures()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		if not frappe.db.exists("Item Group", "All Item Groups"):
			frappe.get_doc(dict(doctype="Item Group", item_group_name="All Item Groups", is_group=1)).insert(
				ignore_permissions=True
			)
		if not frappe.db.exists("Item Group", "COD034 Products"):
			frappe.get_doc(
				dict(
					doctype="Item Group",
					item_group_name="COD034 Products",
					parent_item_group="All Item Groups",
				)
			).insert(ignore_permissions=True)
		if not frappe.db.exists("UOM", "COD034 Unit"):
			frappe.get_doc(dict(doctype="UOM", uom_name="COD034 Unit")).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", "COD034-SOURCE-ITEM"):
			frappe.get_doc(
				dict(
					doctype="Item",
					item_code="COD034-SOURCE-ITEM",
					item_name="COD034 source fixture",
					item_group="COD034 Products",
					stock_uom="COD034 Unit",
					is_stock_item=0,
				)
			).insert(ignore_permissions=True)
		self.cost_center = frappe.db.get_value("HRP Organization Mapping", self.units["A"], "cost_center")
		warehouse = frappe.db.get_value(
			"Warehouse", {"company": source_fixture.COMPANY, "is_group": 0}, "name"
		)
		self.request = frappe.get_doc(
			dict(
				doctype="Material Request",
				company=source_fixture.COMPANY,
				material_request_type="Purchase",
				transaction_date="2026-10-26",
				schedule_date="2026-10-27",
				items=[
					dict(
						item_code="COD034-SOURCE-ITEM",
						qty=1,
						uom="COD034 Unit",
						conversion_factor=1,
						warehouse=warehouse,
						cost_center=self.cost_center,
						schedule_date="2026-10-27",
					)
				],
			)
		).insert(ignore_permissions=True)
		self.line = self.request.items[0].name

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.begin()
		super().tearDown()

	def test_locked_source_exact_amount_mapping_permissions_and_child_change(self):
		# The fixture is a draft ERPNext document; no submitted document or ledger
		# is changed. Setting a string directly demonstrates the DECIMAL driver
		# boundary even when the equivalent Python float cannot preserve it.
		frappe.db.set_value("Material Request Item", self.line, "amount", "999999999999.123456789")
		before = material_request_source.locked_material_request_source(
			self.request.name, self.line, self.units["A"]
		)
		self.assertEqual(before.amount, "999999999999.123456789")
		self.assertEqual(
			(before.company, before.organization_unit, before.cost_center),
			(source_fixture.COMPANY, self.units["A"], self.cost_center),
		)
		self.assertEqual((before.currency, before.docstatus), ("CNY", 0))
		self.assertEqual(len(before.digest()), 64)
		with self.assertRaises(IoneApplicationError):
			material_request_source.locked_material_request_source(
				self.request.name, self.line, self.units["B"]
			)
		with patch.object(material_request_source, "native_read", return_value=False):
			with self.assertRaises(IoneApplicationError):
				material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				)
		frappe.db.set_value("Material Request Item", self.line, "amount", "999999999999.123456788")
		after = material_request_source.locked_material_request_source(
			self.request.name, self.line, self.units["A"]
		)
		self.assertEqual(after.amount, "999999999999.123456788")
		self.assertNotEqual(before.digest(), after.digest())
		frappe.set_user("Guest")
		with self.assertRaises(IoneApplicationError):
			material_request_source.locked_material_request_source(
				self.request.name, self.line, self.units["A"]
			)
