"""Native ERPNext source facts and exact MariaDB decimal reads."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_bench_path

from medops.common.budget_control import budget_control_key, budget_guard_names
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services import material_budget_guards, material_request_source
from medops.hrp_budget.services.control_guards import acquire_budget_guards, assert_budget_guards
from medops.hrp_budget.services.material_budget_guards import acquire_material_budget_guards
from medops.tests import test_budget_consolidation as source_fixture
from medops.tests.test_budget_control_guards import synthetic_key, wait_for_file

INVERTED_LOCK_CHILD = r"""
import json, sys, time
from pathlib import Path
import frappe
from medops.common.budget_control import budget_control_key
from medops.hrp_budget.services.control_guards import acquire_budget_guards, assert_budget_guards
from medops.hrp_budget.services.material_request_source import locked_material_request_source

site, request, line, unit, key_json, mode, directory = sys.argv[1:]
root = Path(directory)
connected = False
result = {'status': 'unexpected'}
def wait_for(name):
    deadline = time.monotonic() + 35
    while not (root / name).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('synthetic coordination timed out')
        time.sleep(0.05)
try:
    frappe.init(site=site, sites_path='.')
    frappe.connect()
    connected = True
    frappe.set_user('Administrator')
    frappe.db.rollback()
    frappe.db.sql('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
    frappe.db.sql('SET SESSION innodb_lock_wait_timeout = 8')
    frappe.db.begin()
    key = budget_control_key(json.loads(key_json))
    if mode == 'source-first':
        locked_material_request_source(request, line, unit)
        (root / 'source-locked').write_text('ready', encoding='utf-8')
        wait_for('guard-locked')
        (root / 'source-waiting').write_text('ready', encoding='utf-8')
        acquire_budget_guards([key])
    else:
        acquire_budget_guards([key])
        (root / 'guard-locked').write_text('ready', encoding='utf-8')
        wait_for('source-waiting')
        # Deliberately bypass the cooperating source-reader ordering check to
        # demonstrate the underlying database risk of acquiring a raw lock late.
        frappe.db.get_value('Material Request', request, 'name', for_update=True)
    assert_budget_guards([key])
    result = {'status': 'success'}
except frappe.QueryDeadlockError:
    result = {'status': 'deadlock'}
except frappe.QueryTimeoutError:
    result = {'status': 'timeout'}
except Exception as error:
    result = {'status': 'unexpected', 'type': type(error).__name__}
finally:
    if connected:
        try:
            frappe.db.rollback()
            result['guard_marker_cleared'] = getattr(frappe.local.db, '_medops_budget_guard_marker', None) is None
        except Exception as error:
            result = {'status': 'rollback_failed', 'type': type(error).__name__}
    frappe.destroy()
    (root / (mode + '-result')).write_text(json.dumps(result), encoding='utf-8')
"""


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

	def test_two_real_purchase_sources_are_locked_before_shared_budget_guard(self):
		second = frappe.copy_doc(self.request).insert(ignore_permissions=True)
		with self.committed_source_fixture():
			primary = frappe.local.db
			try:
				first_source = material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				)
				second_source = material_request_source.locked_material_request_source(
					second.name, second.items[0].name, self.units["A"]
				)
				primary.rollback()
				values = synthetic_key(uuid4().hex)
				values.update(
					company=first_source.company,
					hospital=first_source.hospital,
					organization_unit=first_source.organization_unit,
					project=first_source.project,
				)
				key = budget_control_key(values)
				references = [
					material_budget_guards.MaterialSourceReference(
						source.request, source.line, source.organization_unit, source.digest()
					)
					for source in (second_source, first_source)
				]
				locked = material_budget_guards.acquire_material_budget_guards_for_sources(references, [key])
				self.assertEqual({item.request for item in locked}, {self.request.name, second.name})
				with self.secondary_connection():
					other = frappe.local.db
					other.rollback()
					other.sql("SET SESSION innodb_lock_wait_timeout = 1")
					for name in (self.request.name, second.name):
						with self.subTest(request=name), self.assertRaises(frappe.QueryTimeoutError):
							other.get_value("Material Request", name, "name", for_update=True)
						other.rollback()
				self.assertIs(frappe.local.db, primary)
				assert_budget_guards([key])
			finally:
				frappe.local.db = primary
				primary.rollback()
				if frappe.db.exists("Material Request", second.name):
					frappe.delete_doc("Material Request", second.name, ignore_permissions=True)
				primary.commit()

	def test_generic_budget_guard_denies_late_source_lock(self):
		guard = budget_control_key(synthetic_key(uuid4().hex))
		acquire_budget_guards([guard])
		with patch.object(material_request_source, "native_document") as read_document:
			with self.assertRaises(IoneApplicationError) as denied:
				material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				)
		self.assertEqual(denied.exception.code, "IONE-CORE-0008")
		read_document.assert_not_called()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([guard])

	def test_bound_guard_rejects_a_different_source_before_locking(self):
		source = material_request_source.locked_material_request_source(
			self.request.name, self.line, self.units["A"]
		)
		values = synthetic_key(uuid4().hex)
		values.update(
			company=source.company,
			hospital=source.hospital,
			organization_unit=source.organization_unit,
			project=source.project,
		)
		guard = budget_control_key(values)
		acquire_material_budget_guards(source, [guard])
		with patch.object(material_request_source, "native_document") as read_document:
			with self.assertRaises(IoneApplicationError) as denied:
				material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["B"]
				)
		self.assertEqual(denied.exception.code, "IONE-CORE-0008")
		read_document.assert_not_called()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([guard])

	def test_source_lock_rollback_invalidates_bound_guard_before_relocking(self):
		frappe.db.savepoint("cod034_before_material_source")
		source = material_request_source.locked_material_request_source(
			self.request.name, self.line, self.units["A"]
		)
		values = synthetic_key(uuid4().hex)
		values.update(
			company=source.company,
			hospital=source.hospital,
			organization_unit=source.organization_unit,
			project=source.project,
		)
		guard = budget_control_key(values)
		acquire_material_budget_guards(source, [guard])
		frappe.db.rollback(save_point="cod034_before_material_source")
		with patch.object(material_request_source, "native_document") as read_document:
			with self.assertRaises(IoneApplicationError) as denied:
				material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				)
		self.assertEqual(denied.exception.code, "IONE-CORE-0008")
		read_document.assert_not_called()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([guard])

	def test_reversed_source_and_guard_lock_order_deadlocks_and_rolls_back(self):
		"""A late source lock can deadlock a transaction that locked source first."""
		values = synthetic_key(uuid4().hex)
		guard = budget_control_key(values)
		names = budget_guard_names([guard])
		processes = []
		with self.committed_source_fixture():
			primary = frappe.local.db
			before = material_request_source.locked_material_request_source(
				self.request.name, self.line, self.units["A"]
			).digest()
			primary.rollback()
			for name in names:
				self.assertEqual(
					primary.sql("SELECT COUNT(*) FROM `tabSeries` WHERE `name`=%s", (name,))[0][0], 0
				)
			with tempfile.TemporaryDirectory(prefix="medops-source-guard-order-") as directory:
				root = Path(directory)

				def launch(mode):
					process = subprocess.Popen(
						[
							sys.executable,
							"-c",
							INVERTED_LOCK_CHILD,
							frappe.local.site,
							self.request.name,
							self.line,
							self.units["A"],
							json.dumps(values),
							mode,
							directory,
						],
						cwd=os.path.join(get_bench_path(), "sites"),
						stdin=subprocess.DEVNULL,
						stdout=subprocess.DEVNULL,
						stderr=subprocess.DEVNULL,
					)
					processes.append(process)
					return process

				try:
					source_first = launch("source-first")
					wait_for_file(root / "source-locked", source_first, timeout=35)
					guard_first = launch("guard-first")
					wait_for_file(root / "guard-locked", guard_first, timeout=35)
					self.assertEqual(source_first.wait(timeout=35), 0)
					self.assertEqual(guard_first.wait(timeout=35), 0)
					outcomes = [
						json.loads((root / f"{mode}-result").read_text(encoding="utf-8"))
						for mode in ("source-first", "guard-first")
					]
					self.assertCountEqual([item["status"] for item in outcomes], ["deadlock", "success"])
					self.assertTrue(all(item.get("guard_marker_cleared") for item in outcomes))
				finally:
					for process in processes:
						if process.poll() is None:
							process.terminate()
							try:
								process.wait(timeout=5)
							except subprocess.TimeoutExpired:
								process.kill()
								process.wait(timeout=5)
			primary.rollback()
			for name in names:
				self.assertEqual(
					primary.sql("SELECT COUNT(*) FROM `tabSeries` WHERE `name`=%s", (name,))[0][0], 0
				)
			self.assertEqual(
				material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				).digest(),
				before,
			)
			primary.rollback()

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
