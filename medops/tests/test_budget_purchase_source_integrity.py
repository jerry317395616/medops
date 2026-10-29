"""Native PO writes with connection-private Decision presence, not financial authorization.

The separate disposable committed probe must verify real independent evidence.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests.utils import FrappeTestCase

from medops.common.budget_control import budget_control_key
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services import purchase_source_integrity as integrity
from medops.hrp_budget.services.control_guards import (
	acquire_budget_guards,
	acquire_multi_source_bound_budget_guards,
	assert_budget_guards,
)
from medops.tests import test_budget_consolidation as fixtures
from medops.tests.test_budget_control_guards import synthetic_key


class TestBudgetPurchaseSourceIntegrity(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_, cls.units, _, _ = fixtures.fixtures()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.rollback()
		if "guard" in self._testMethodName:
			frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		else:
			frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.begin()
		self.addCleanup(self.drop_shadow)
		frappe.db.sql(
			"CREATE TEMPORARY TABLE `medops_purchase_binding_template` LIKE `tabHRP Budget Commitment Authorization`"
		)
		frappe.db.sql(
			"CREATE TEMPORARY TABLE `tabHRP Budget Commitment Authorization` LIKE `medops_purchase_binding_template`"
		)
		frappe.db.sql("DROP TEMPORARY TABLE `medops_purchase_binding_template`")
		tag = "PURCHASE-EDIT-" + uuid4().hex[:12]
		if not frappe.db.exists("Item Group", "All Item Groups"):
			frappe.get_doc(dict(doctype="Item Group", item_group_name="All Item Groups", is_group=1)).insert()
		frappe.get_doc(
			dict(doctype="Item Group", item_group_name=tag, parent_item_group="All Item Groups")
		).insert()
		frappe.get_doc(dict(doctype="UOM", uom_name=tag)).insert()
		for code in (tag, tag + "-2"):
			frappe.get_doc(
				dict(
					doctype="Item",
					item_code=code,
					item_name=code,
					item_group=tag,
					stock_uom=tag,
					is_stock_item=0,
				)
			).insert()
		center = frappe.db.get_value("HRP Organization Mapping", self.units["A"], "cost_center")
		warehouse = frappe.db.get_value("Warehouse", {"company": fixtures.COMPANY, "is_group": 0}, "name")
		account = frappe.db.get_value(
			"Account", {"company": fixtures.COMPANY, "is_group": 0, "root_type": "Expense"}, "name"
		)
		self.request = frappe.get_doc(
			dict(
				doctype="Material Request",
				company=fixtures.COMPANY,
				material_request_type="Purchase",
				transaction_date="2026-10-26",
				schedule_date="2026-10-27",
				items=[
					dict(
						item_code=code,
						qty=1,
						rate=10,
						amount=10,
						uom=tag,
						conversion_factor=1,
						warehouse=warehouse,
						cost_center=center,
						expense_account=account,
						schedule_date="2026-10-27",
					)
					for code in (tag, tag + "-2")
				],
			)
		).insert()
		self.request.submit()
		if not frappe.db.exists("Supplier Group", "All Supplier Groups"):
			frappe.get_doc(
				dict(doctype="Supplier Group", supplier_group_name="All Supplier Groups", is_group=1)
			).insert()
		supplier = frappe.get_doc(
			dict(
				doctype="Supplier",
				supplier_name=tag,
				supplier_type="Company",
				supplier_group="All Supplier Groups",
				country="China",
			)
		).insert()
		self.order = frappe.get_doc(
			dict(
				doctype="Purchase Order",
				company=fixtures.COMPANY,
				supplier=supplier.name,
				transaction_date="2026-10-26",
				schedule_date="2026-10-27",
				currency="CNY",
				conversion_rate=1,
				items=[
					dict(
						item_code=row.item_code,
						qty=row.qty,
						rate=row.rate,
						uom=row.uom,
						stock_uom=row.stock_uom or row.uom,
						conversion_factor=1,
						warehouse=row.warehouse,
						cost_center=row.cost_center,
						expense_account=row.expense_account,
						schedule_date="2026-10-27",
						material_request=self.request.name,
						material_request_item=row.name,
					)
					for row in self.request.items
				],
			)
		).insert()
		self.order.submit()
		self.line = self.order.items[0].name
		self.free_line = self.order.items[1].name

	def drop_shadow(self):
		frappe.db.rollback()
		frappe.db.sql("DROP TEMPORARY TABLE IF EXISTS `tabHRP Budget Commitment Authorization`")
		frappe.db.sql("DROP TEMPORARY TABLE IF EXISTS `medops_purchase_binding_template`")

	def tearDown(self):
		self.drop_shadow()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		super().tearDown()

	def bind(self, kind="Decision", *, line=None):
		frappe.db.sql(
			"INSERT INTO `tabHRP Budget Commitment Authorization` "
			"(name, docstatus, event_kind, purchase_order, purchase_order_line) VALUES (%s,0,%s,%s,%s)",
			(uuid4().hex * 2, kind, self.order.name, line or self.line),
		)

	def persisted(self):
		return frappe.db.sql(
			"SELECT name, parent, docstatus, CAST(qty AS CHAR), CAST(rate AS CHAR), "
			"CAST(amount AS CHAR), CAST(base_amount AS CHAR), material_request, material_request_item "
			"FROM `tabPurchase Order Item` WHERE parent=%s ORDER BY name",
			(self.order.name,),
		)

	def test_unbound_and_confirmation_only_native_changes_remain_available(self):
		self.bind("Confirmation")
		self.order.update_status("On Hold")
		self.order.reload()
		self.order.update_status("Draft")
		frappe.get_doc("Purchase Order Item", self.line).db_set({"rate": 11, "amount": 11, "base_amount": 11})
		self.order.reload()
		self.order.cancel()
		self.assertEqual(frappe.db.get_value("Purchase Order", self.order.name, "docstatus"), 2)

	def test_unbound_draft_deletion_and_new_child_save_are_native(self):
		draft = frappe.copy_doc(self.order)
		draft.docstatus = 0
		for row in draft.items:
			row.docstatus = 0
		draft.insert()
		fresh = frappe.copy_doc(draft.items[0])
		fresh.docstatus = 0
		fresh.parent = draft.name
		fresh.save()
		self.assertTrue(frappe.db.exists("Purchase Order Item", fresh.name))
		name = draft.name
		draft.delete()
		self.assertFalse(frappe.db.exists("Purchase Order", name))
		child = frappe.get_doc("Purchase Order Item", self.free_line)
		child.flags.ignore_validate_update_after_submit = True
		child.description = "normal native note"
		child.save()
		self.assertEqual(
			frappe.db.get_value("Purchase Order Item", child.name, "description"), child.description
		)

	def test_bound_parent_commercial_db_set_cannot_rewrite_evidence(self):
		self.bind()
		before = self.persisted()
		for field, value in (
			("supplier", "Other"),
			("currency", "USD"),
			("conversion_rate", 2),
			("transaction_date", "2026-10-28"),
		):
			doc = frappe.get_doc("Purchase Order", self.order.name)
			with self.subTest(field=field), self.assertRaises(IoneApplicationError):
				doc.db_set(field, value)
		self.assertEqual(self.persisted(), before)

	def test_bound_independent_child_save_is_rejected_before_parent_reload(self):
		self.bind()
		before = self.persisted()
		for field, value in (("qty", 2), ("rate", 11), ("material_request_item", self.request.items[1].name)):
			child = frappe.get_doc("Purchase Order Item", self.line)
			child.flags.ignore_validate_update_after_submit = True
			child.set(field, value)
			with self.subTest(field=field), self.assertRaises(IoneApplicationError):
				child.save(ignore_permissions=True)
			self.assertEqual(self.persisted(), before)

	def test_native_update_items_cannot_change_bound_price(self):
		from erpnext.controllers.accounts_controller import update_child_qty_rate

		self.bind()
		before = self.persisted()
		rows = [
			dict(
				docname=row.name,
				item_code=row.item_code,
				qty=row.qty,
				rate=11 if row.name == self.line else row.rate,
				uom=row.uom,
				conversion_factor=row.conversion_factor,
				schedule_date=str(row.schedule_date),
				description=row.description,
			)
			for row in self.order.items
		]
		with self.assertRaises(IoneApplicationError):
			update_child_qty_rate("Purchase Order", rows, self.order.name)
		self.assertEqual(self.persisted(), before)

	def test_bound_child_db_set_even_pretended_new_cannot_commit(self):
		self.bind()
		before = self.persisted()
		child = frappe.get_doc("Purchase Order Item", self.line)
		child.set("__islocal", 1)
		with self.assertRaises(IoneApplicationError):
			child.db_set({"rate": 11, "amount": 11, "base_amount": 11}, commit=True)
		self.assertEqual(self.persisted(), before)

	def test_noncanonical_names_cannot_alias_a_bound_native_row(self):
		self.bind()
		before = self.persisted()
		for doctype, name, field, value in (
			("Purchase Order Item", self.line, "rate", 11),
			("Purchase Order", self.order.name, "supplier", "Other"),
		):
			doc = frappe.get_doc(doctype, name)
			doc.set("__islocal", 1)
			doc.name += " "
			with self.subTest(doctype=doctype), self.assertRaises(IoneApplicationError):
				doc.db_set(field, value)
		self.assertEqual(self.persisted(), before)

	def test_actual_decision_on_draft_is_not_treated_as_unbound(self):
		draft = frappe.copy_doc(self.order)
		draft.docstatus = 0
		for row in draft.items:
			row.docstatus = 0
			row.material_request = None
			row.material_request_item = None
		draft.insert()
		self.order, self.line = draft, draft.items[0].name
		self.bind()
		with self.assertRaises(IoneApplicationError):
			draft.submit()
		with self.assertRaises(IoneApplicationError):
			frappe.delete_doc("Purchase Order", draft.name, force=True)
		self.assertEqual(frappe.db.get_value("Purchase Order", draft.name, "docstatus"), 0)

	def test_bound_parent_and_child_cancel_delete_rename_are_refused(self):
		self.bind()
		self.order.flags.ignore_links = True
		with self.assertRaises(IoneApplicationError):
			self.order.cancel()
		with self.assertRaises(IoneApplicationError):
			frappe.rename_doc("Purchase Order", self.order.name, "BOUND-PO-RENAMED", force=True)
		with self.assertRaises(IoneApplicationError):
			self.order.run_method("on_trash")
		child = frappe.get_doc("Purchase Order Item", self.line)
		with self.assertRaises(IoneApplicationError):
			child.cancel()
		with self.assertRaises(IoneApplicationError):
			frappe.delete_doc("Purchase Order Item", self.line, force=True)
		with self.assertRaises(IoneApplicationError):
			frappe.rename_doc("Purchase Order Item", self.line, "BOUND-PO-LINE-RENAMED", force=True)
		self.assertEqual(frappe.db.get_value("Purchase Order", self.order.name, "docstatus"), 1)

	def test_bound_native_close_hold_reopen_and_progress_remain_allowed(self):
		from erpnext.buying.doctype.purchase_order.purchase_order import close_or_unclose_purchase_orders

		self.bind()
		before = self.persisted()
		close_or_unclose_purchase_orders([self.order.name], "Closed")
		self.assertEqual(frappe.db.get_value("Purchase Order", self.order.name, "status"), "Closed")
		close_or_unclose_purchase_orders([self.order.name], "Draft")
		self.order.reload()
		self.order.update_status("On Hold")
		self.order.reload()
		self.order.update_status("Draft")
		for status in ("To Bill", "To Receive", "Completed", "Delivered", "To Pay"):
			frappe.get_doc("Purchase Order", self.order.name).db_set("status", status)
		frappe.get_doc("Purchase Order Item", self.line).db_set({"received_qty": 1, "billed_amt": 10})
		self.assertEqual(self.persisted(), before)

	def test_unbound_line_economics_and_bound_description_remain_available(self):
		self.bind()
		before = self.persisted()
		child = frappe.get_doc("Purchase Order Item", self.free_line)
		child.flags.ignore_validate_update_after_submit = True
		child.rate = 12
		child.save(ignore_permissions=True)
		frappe.get_doc("Purchase Order Item", self.line).db_set("description", "native note")
		after = self.persisted()
		self.assertEqual([r for r in before if r[0] == self.line], [r for r in after if r[0] == self.line])
		self.assertNotEqual(before, after)

	def test_missing_bound_line_and_bounded_inventory_fail_closed(self):
		self.bind(line="missing-native-line")
		with self.assertRaises(IoneApplicationError):
			self.order.db_set("status", "Closed")
		with patch.object(integrity, "MAX_PARENT_BINDINGS", 0), self.assertRaises(IoneApplicationError):
			integrity.protect_purchase_order_edit(self.order)

	def test_generic_guard_rejects_hook_before_its_own_late_parent_query(self):
		key = budget_control_key(synthetic_key(uuid4().hex))
		acquire_budget_guards([key])
		with patch.object(frappe.db, "sql") as sql, self.assertRaises(IoneApplicationError):
			integrity.protect_purchase_order_edit(self.order)
		sql.assert_not_called()

	def test_missing_binding_table_only_passes_during_install_or_migration(self):
		with patch.object(frappe.db, "table_exists", return_value=False):
			with patch.dict(frappe.flags, {"in_install": False, "in_migrate": False}):
				with self.assertRaises(IoneApplicationError):
					integrity.protect_purchase_order_edit(self.order)
			for flag in ("in_install", "in_migrate"):
				with patch.dict(frappe.flags, {"in_install": False, "in_migrate": False, flag: True}):
					integrity.protect_purchase_order_edit(self.order)

	def test_child_membership_is_rechecked_after_discovery(self):
		self.bind()
		child = frappe.get_doc("Purchase Order Item", self.line)
		query = frappe.db.sql

		def moved_child(statement, *args, **kwargs):
			if statement.startswith("SELECT parent, parenttype, parentfield FROM `tabPurchase Order Item`"):
				return [frappe._dict(parent="Other", parenttype="Purchase Order", parentfield="items")]
			return query(statement, *args, **kwargs)

		with patch.object(frappe.db, "sql", side_effect=moved_child), self.assertRaises(IoneApplicationError):
			integrity.protect_purchase_order_item_edit(child)

	def test_hook_errors_and_deadlocks_invalidate_guard_until_full_rollback(self):
		key = budget_control_key(synthetic_key(uuid4().hex))
		for error in (frappe.QueryDeadlockError, frappe.QueryTimeoutError, ValueError, RuntimeError):
			frappe.db.rollback()
			frappe.db.begin()
			acquire_budget_guards([key])
			expected = RuntimeError if error is RuntimeError else IoneApplicationError
			with self.subTest(error=error.__name__):
				with patch.object(integrity, "_parent", side_effect=error("test failure")):
					with self.assertRaises(expected):
						integrity.protect_purchase_order_edit(self.order)
				with self.assertRaises(IoneApplicationError):
					assert_budget_guards([key])

	def test_prebound_guard_progress_preserves_witness_then_poison_blocks_revisit(self):
		self.bind()
		key = budget_control_key(synthetic_key(uuid4().hex))
		frappe.db.get_value("Material Request", self.request.name, "name", for_update=True)
		frappe.db.get_value("Purchase Order", self.order.name, "name", for_update=True)
		acquire_multi_source_bound_budget_guards(
			[key],
			source_bindings=((self.request.name, self.request.items[0].name, self.units["A"], "b" * 64),),
			purchase_parents=(self.order.name,),
		)
		self.order.db_set("status", "Closed")
		assert_budget_guards([key])
		with self.assertRaises(IoneApplicationError):
			frappe.get_doc("Purchase Order Item", self.line).db_set("rate", 11)
		with self.assertRaises(IoneApplicationError):
			integrity.protect_purchase_order_edit(self.order)
