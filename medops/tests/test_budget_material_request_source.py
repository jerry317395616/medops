"""Native ERPNext source facts and exact MariaDB decimal reads."""

from __future__ import annotations

from contextlib import contextmanager
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
		self.created_root = not frappe.db.exists("Item Group", "All Item Groups")
		self.created_group = not frappe.db.exists("Item Group", "COD034 Products")
		self.created_uom = not frappe.db.exists("UOM", "COD034 Unit")
		self.created_item = not frappe.db.exists("Item", "COD034-SOURCE-ITEM")
		if self.created_root:
			frappe.get_doc(dict(doctype="Item Group", item_group_name="All Item Groups", is_group=1)).insert(
				ignore_permissions=True
			)
		if self.created_group:
			frappe.get_doc(
				dict(
					doctype="Item Group",
					item_group_name="COD034 Products",
					parent_item_group="All Item Groups",
				)
			).insert(ignore_permissions=True)
		if self.created_uom:
			frappe.get_doc(dict(doctype="UOM", uom_name="COD034 Unit")).insert(ignore_permissions=True)
		if self.created_item:
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
		self.account = frappe.db.get_value(
			"Account",
			{
				"company": source_fixture.COMPANY,
				"is_group": 0,
				"disabled": 0,
				"report_type": "Profit and Loss",
			},
			"name",
		)
		self.assertTrue(self.account)
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
						expense_account=self.account,
						schedule_date="2026-10-27",
					)
				],
			)
		).insert(ignore_permissions=True)
		self.line = self.request.items[0].name

	@contextmanager
	def committed_source_fixture(self):
		"""Publish only synthetic draft fixtures and remove the ones we created."""
		primary = frappe.local.db
		primary.commit()
		try:
			# The pinned lazy secondary connector changes frappe.local.db while
			# creating the connection. Restore the original explicitly.
			try:
				with self.secondary_connection():
					self._secondary_connection.rollback()
			finally:
				frappe.local.db = primary
			primary.rollback()
			primary.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
			primary.begin()
			yield
		finally:
			frappe.local.db = primary
			frappe.set_user("Administrator")
			primary.rollback()
			for doctype, name, owned in (
				("Material Request", self.request.name, True),
				("Item", "COD034-SOURCE-ITEM", self.created_item),
				("Item Group", "COD034 Products", self.created_group),
				("UOM", "COD034 Unit", self.created_uom),
				("Item Group", "All Item Groups", self.created_root),
			):
				if owned and frappe.db.exists(doctype, name):
					frappe.delete_doc(doctype, name, ignore_permissions=True)
			primary.commit()
			primary.begin()

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
		self.assertEqual(before.expense_account, self.account)
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
		group = frappe.db.get_value("Account", {"company": source_fixture.COMPANY, "is_group": 1}, "name")
		self.assertTrue(group)
		frappe.db.set_value("Material Request Item", self.line, "expense_account", group)
		with self.assertRaises(IoneApplicationError):
			material_request_source.locked_material_request_source(
				self.request.name, self.line, self.units["A"]
			)
		frappe.db.set_value("Material Request Item", self.line, "expense_account", self.account)
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

	def test_other_transaction_cannot_change_locked_parent_or_child(self):
		with self.committed_source_fixture():
			primary = frappe.local.db
			before = material_request_source.locked_material_request_source(
				self.request.name, self.line, self.units["A"]
			)
			with self.secondary_connection():
				other = frappe.local.db
				other.rollback()
				other.sql("SET SESSION innodb_lock_wait_timeout = 1")
				for doctype, name, field, value in (
					("Material Request", self.request.name, "title", "concurrent change"),
					("Material Request Item", self.line, "amount", "10.000000000"),
				):
					with self.subTest(doctype=doctype), self.assertRaises(frappe.QueryTimeoutError):
						other.set_value(doctype, name, field, value)
					other.rollback()
			self.assertIs(frappe.local.db, primary)
			self.assertEqual(
				material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				).digest(),
				before.digest(),
			)
			primary.rollback()
			with self.secondary_connection():
				other = frappe.local.db
				other.set_value("Material Request Item", self.line, "amount", "10.000000000")
				other.rollback()

	def test_role_revocation_from_another_transaction_denies_source(self):
		manager = source_fixture.MANAGER
		with self.committed_source_fixture():
			primary = frappe.local.db
			self.assertTrue(
				frappe.db.exists(
					"Has Role", {"parenttype": "User", "parent": manager, "role": "HRP Budget Manager"}
				)
			)
			had_purchase = bool(
				frappe.db.exists(
					"Has Role", {"parenttype": "User", "parent": manager, "role": "Purchase User"}
				)
			)
			user = frappe.get_doc("User", manager)
			user.add_roles("Purchase User")
			primary.commit()
			calls = []
			original = material_request_source.native_document
			try:
				frappe.set_user(manager)
				# First prove the synthetic actor can read this complete source.
				material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				)
				primary.rollback()

				def revoke_after_item(doctype, *args, **kwargs):
					doc = original(doctype, *args, **kwargs)
					if doctype == "Item" and kwargs.get("lock") and not calls:
						calls.append(1)
						with self.secondary_connection():
							frappe.set_user("Administrator")
							try:
								frappe.get_doc("User", manager).remove_roles("HRP Budget Manager")
								self._secondary_connection.commit()
							except Exception:
								self._secondary_connection.rollback()
								raise
						frappe.set_user(manager)
					return doc

				with patch.object(material_request_source, "native_document", side_effect=revoke_after_item):
					with self.assertRaises(IoneApplicationError) as denied:
						material_request_source.locked_material_request_source(
							self.request.name, self.line, self.units["A"]
						)
				self.assertEqual(denied.exception.code, "IONE-CORE-0002")
				self.assertEqual(calls, [1])
			finally:
				frappe.local.db = primary
				primary.rollback()
				frappe.set_user("Administrator")
				user = frappe.get_doc("User", manager)
				user.add_roles("HRP Budget Manager")
				if not had_purchase:
					user.remove_roles("Purchase User")
				primary.commit()
				frappe.cache.hdel("roles", manager)
