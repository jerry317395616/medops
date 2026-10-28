"""Native MR controllers with connection-private binding presence, not financial authorization.

The committed probe is the counterpart with fully valid reservation evidence.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests.utils import FrappeTestCase

from medops.common.budget_control import budget_control_key
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services import material_source_integrity as integrity
from medops.hrp_budget.services.control_guards import (
	acquire_budget_guards,
	acquire_source_bound_budget_guards,
	assert_budget_guards,
)
from medops.tests import test_budget_consolidation as fixtures
from medops.tests.test_budget_control_guards import synthetic_key


class TestBudgetMaterialSourceIntegrity(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_, cls.units, _, _ = fixtures.fixtures()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.rollback()
		if self._testMethodName in {
			"test_generic_budget_guard_rejects_the_parent_before_any_late_source_query",
			"test_prebound_parent_progress_preserves_the_real_transaction_witness",
		}:
			frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		else:
			frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.begin()
		self.addCleanup(self.drop_shadow)
		frappe.db.sql(
			"CREATE TEMPORARY TABLE `medops_source_binding_template` LIKE `tabHRP Budget Reservation`"
		)
		frappe.db.sql(
			"CREATE TEMPORARY TABLE `tabHRP Budget Reservation` LIKE `medops_source_binding_template`"
		)
		frappe.db.sql("DROP TEMPORARY TABLE `medops_source_binding_template`")
		tag = "SOURCE-EDIT-" + uuid4().hex[:12]
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
						schedule_date="2026-10-27",
					)
					for code in (tag, tag + "-2")
				],
			)
		).insert()
		self.request.submit()
		self.line = self.request.items[0].name
		self.free_line = self.request.items[1].name

	def drop_shadow(self):
		frappe.db.rollback()
		frappe.db.sql("DROP TEMPORARY TABLE IF EXISTS `tabHRP Budget Reservation`")
		frappe.db.sql("DROP TEMPORARY TABLE IF EXISTS `medops_source_binding_template`")

	def tearDown(self):
		self.drop_shadow()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		super().tearDown()

	def bind(self):
		frappe.db.sql(
			"INSERT INTO `tabHRP Budget Reservation` (name, docstatus, source_doctype, source_name, source_line) "
			"VALUES (%s,1,'Material Request',%s,%s)",
			("a" * 64, self.request.name, self.line),
		)

	def persisted(self):
		return frappe.db.sql(
			"SELECT name, CAST(qty AS CHAR), CAST(rate AS CHAR), CAST(amount AS CHAR), cost_center, project "
			"FROM `tabMaterial Request Item` WHERE parent=%s ORDER BY name",
			(self.request.name,),
		)

	def test_unbound_native_stop_and_reopen_remain_available(self):
		self.request.update_status("Stopped")
		self.assertEqual(frappe.db.get_value("Material Request", self.request.name, "status"), "Stopped")
		self.request.reload()
		self.request.update_status("Submitted")
		self.assertNotEqual(frappe.db.get_value("Material Request", self.request.name, "status"), "Stopped")

	def test_bound_stop_and_even_ignore_links_cancel_cannot_orphan_history(self):
		self.bind()
		before = self.persisted()
		with self.assertRaises(IoneApplicationError):
			self.request.update_status("Stopped")
		pretended_new = frappe.get_doc("Material Request", self.request.name)
		pretended_new.set("__islocal", 1)
		with self.assertRaises(IoneApplicationError):
			pretended_new.db_set("status", "Stopped")
		self.request.reload()
		self.request.flags.ignore_links = True
		with self.assertRaises(IoneApplicationError):
			self.request.cancel()
		self.assertEqual(frappe.db.get_value("Material Request", self.request.name, "docstatus"), 1)
		self.assertEqual(self.persisted(), before)

	def test_trusted_native_submit_edit_flag_does_not_rewrite_bound_economics(self):
		self.bind()
		before = self.persisted()
		for field, value in (("qty", 2), ("rate", 11), ("amount", 11)):
			doc = frappe.get_doc("Material Request", self.request.name)
			doc.flags.ignore_validate_update_after_submit = True
			doc.items[0].set(field, value)
			with self.subTest(field=field), self.assertRaises(IoneApplicationError):
				doc.save()
			self.assertEqual(self.persisted(), before)

	def test_bound_child_db_set_is_stopped_before_its_optional_commit(self):
		self.bind()
		before = self.persisted()
		child = frappe.get_doc("Material Request Item", self.line)
		child.set("__islocal", 1)
		with self.assertRaises(IoneApplicationError):
			child.db_set({"rate": 11, "amount": 11}, commit=True)
		self.assertEqual(self.persisted(), before)

	def test_native_progress_and_unbound_line_updates_preserve_the_bound_line(self):
		self.bind()
		before = self.persisted()
		self.request.db_set("status", "Ordered")
		frappe.get_doc("Material Request Item", self.free_line).db_set({"rate": 12, "amount": 12})
		after = self.persisted()
		self.assertEqual(
			[row for row in before if row[0] == self.line], [row for row in after if row[0] == self.line]
		)
		self.assertNotEqual(before, after)
		self.assertEqual(frappe.db.get_value("Material Request", self.request.name, "status"), "Ordered")

	def test_native_parent_rename_and_bound_child_lifecycle_are_refused(self):
		self.bind()
		with self.assertRaises(IoneApplicationError):
			frappe.rename_doc("Material Request", self.request.name, "SOURCE-RENAMED", force=True)
		child = frappe.get_doc("Material Request Item", self.line)
		for method in ("on_trash", "before_rename"):
			with self.subTest(method=method), self.assertRaises(IoneApplicationError):
				integrity.protect_material_request_item_lifecycle(child, method)
		self.assertEqual(frappe.db.get_value(integrity.ROOT, "a" * 64, "source_name"), self.request.name)

	def test_generic_budget_guard_rejects_the_parent_before_any_late_source_query(self):
		key = budget_control_key(synthetic_key(uuid4().hex))
		acquire_budget_guards([key])
		with patch.object(frappe.db, "sql") as sql, self.assertRaises(IoneApplicationError):
			integrity.protect_material_request_edit(self.request)
		sql.assert_not_called()

	def test_prebound_parent_progress_preserves_the_real_transaction_witness(self):
		self.bind()
		key = budget_control_key(synthetic_key(uuid4().hex))
		frappe.db.get_value("Material Request", self.request.name, "name", for_update=True)
		acquire_source_bound_budget_guards(
			[key], source_binding=(self.request.name, self.line, self.units["A"], "b" * 64)
		)
		self.request.db_set("status", "Ordered")
		assert_budget_guards([key])
