"""Purchase-order source ordering and permission regressions with synthetic facts."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.budget_control import budget_control_key
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services import purchase_order_source as adapter
from medops.hrp_budget.services.control_guards import acquire_budget_guards, assert_budget_guards
from medops.hrp_budget.services.material_request_source import MaterialRequestSource
from medops.tests.test_budget_control_sources import input_key


class TestBudgetPurchaseOrderSource(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		self.key = budget_control_key(input_key(company="company-" + uuid4().hex))
		self.source = MaterialRequestSource(
			request="request-a",
			line="request-line",
			request_modified="2026-01-01 00:00:00",
			line_modified="2026-01-01 00:00:00",
			company=self.key.company,
			hospital=self.key.hospital,
			organization_unit=self.key.organization_unit,
			organization_version="version",
			mapping_revision=1,
			cost_center="center",
			currency="CNY",
			transaction_date="2026-01-15",
			docstatus=1,
			status="Submitted",
			item_code="item",
			uom="unit",
			expense_account="account",
			project=None,
			quantity="5.000000000",
			rate="6.000000000",
			amount="30.000000000",
		)
		self.item = SimpleNamespace(
			name="order-line",
			modified="2026-01-16 00:00:00",
			material_request=self.source.request,
			material_request_item=self.source.line,
			item_code="item",
			uom="unit",
			cost_center="center",
			expense_account="account",
			project=None,
			is_free_item=0,
		)
		self.order = SimpleNamespace(
			name="order-a",
			modified="2026-01-16 00:00:00",
			docstatus=1,
			status="To Receive and Bill",
			items=[self.item],
			company=self.source.company,
			currency="CNY",
			supplier="supplier",
			transaction_date="2026-01-16",
		)
		self.values = {
			"conversion_rate": "1.000000000",
			"qty": "2.000000000",
			"rate": "6.000000000",
			"amount": "12.000000000",
			"base_amount": "12.000000000",
		}

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.begin()
		super().tearDown()

	def read(self, *, native_effect=None, source_effect=None, sql_effect=None, transaction_state=None):
		calls = []

		def native(doctype, name, fields, *, lock=False):
			calls.append((doctype, name, lock))
			if native_effect:
				return native_effect(doctype, name, lock)
			return SimpleNamespace(name=name) if doctype == "Material Request" else self.order

		def source(*args):
			return source_effect() if source_effect else self.source

		def sql(*args, **kwargs):
			if args[0] == "SELECT @@tx_isolation, @@autocommit":
				return transaction_state if transaction_state is not None else [("READ-COMMITTED", 0)]
			return sql_effect() if sql_effect else [self.values]

		with (
			patch.object(adapter, "native_document", side_effect=native),
			patch.object(adapter, "locked_material_request_source", side_effect=source),
			patch.object(adapter, "require_roles"),
			patch("medops.hrp_budget.services.release_execution.refresh_financial_authority"),
			patch.object(frappe.db, "sql", side_effect=sql),
		):
			result = adapter.locked_purchase_order_source(
				"order-a",
				"order-line",
				self.source.request,
				self.source.line,
				self.source.organization_unit,
				expected_request_digest=self.source.digest(),
			)
		return result, calls

	def test_parents_are_prelocked_before_material_source_and_order_rechecked(self):
		(source, order), calls = self.read()
		self.assertEqual(source, self.source)
		self.assertEqual(
			(order.order, order.line, order.quantity, order.amount),
			("order-a", "order-line", "2.000000000", "12.000000000"),
		)
		self.assertEqual(
			calls[:4],
			[
				("Material Request", "request-a", False),
				("Purchase Order", "order-a", False),
				("Material Request", "request-a", True),
				("Purchase Order", "order-a", True),
			],
		)
		self.assertEqual(calls[4:], [("Purchase Order", "order-a", True)] * 2)

	def test_commitment_identity_tracks_order_line_not_snapshot_revision(self):
		(_, order), _ = self.read()
		changed = replace(order, order_modified="2026-01-17 00:00:00", amount="13.000000000")
		self.assertNotEqual(order.digest(), changed.digest())
		self.assertEqual(order.commitment_identity(), changed.commitment_identity())
		self.assertNotEqual(order.commitment_identity(), replace(order, line="other").commitment_identity())
		with self.assertRaises(ValueError):
			replace(order, line=" other ").commitment_identity()

	def test_wrong_material_link_item_and_account_are_rejected(self):
		for field, value in (
			("material_request", "another"),
			("material_request_item", "another"),
			("item_code", "other"),
			("uom", "other"),
			("cost_center", "other"),
			("expense_account", "other"),
			("project", "other"),
			("is_free_item", 1),
		):
			original = getattr(self.item, field)
			setattr(self.item, field, value)
			with self.subTest(field=field), self.assertRaises(IoneApplicationError):
				self.read()
			setattr(self.item, field, original)

	def test_draft_closed_and_cross_currency_orders_are_rejected(self):
		for field, value in (
			("docstatus", 0),
			("status", "Closed"),
			("currency", "USD"),
			("company", "other"),
		):
			original = getattr(self.order, field)
			setattr(self.order, field, value)
			with self.subTest(field=field), self.assertRaises(IoneApplicationError):
				self.read()
			setattr(self.order, field, original)

	def test_invalid_quantity_amount_and_exchange_rate_are_rejected(self):
		for field, value in (
			("qty", "0"),
			("qty", "6"),
			("amount", "0"),
			("base_amount", "13"),
			("conversion_rate", "2"),
		):
			original = self.values[field]
			self.values[field] = value
			with self.subTest(field=field), self.assertRaises(IoneApplicationError):
				self.read()
			self.values[field] = original

	def test_changed_material_source_is_rejected(self):
		values = iter([self.source, replace(self.source, amount="31.000000000")])
		with self.assertRaises(IoneApplicationError):
			self.read(source_effect=lambda: next(values))

	def test_unsubmitted_material_request_is_rejected(self):
		original = self.source
		self.source = replace(original, docstatus=0)
		with self.assertRaises(IoneApplicationError):
			self.read()

	def test_actor_change_during_order_read_is_rejected(self):
		def change_actor():
			frappe.set_user("Guest")
			return [self.values]

		with self.assertRaises(IoneApplicationError):
			self.read(sql_effect=change_actor)

	def test_incompatible_transaction_is_rejected_before_native_source_reads(self):
		calls = []

		def native(doctype, name, lock):
			calls.append((doctype, name, lock))
			return SimpleNamespace(name=name)

		for state in ([("REPEATABLE-READ", 0)], [("READ-COMMITTED", 1)], []):
			with self.subTest(state=state), self.assertRaises(IoneApplicationError):
				self.read(native_effect=native, transaction_state=state)
		self.assertEqual(calls, [])

	def test_permission_failure_occurs_before_parent_lock(self):
		seen = []

		def deny(doctype, name, lock):
			seen.append((doctype, lock))
			if doctype == "Purchase Order":
				raise PermissionError("synthetic denial")
			return SimpleNamespace(name=name)

		with self.assertRaises(PermissionError):
			self.read(native_effect=deny)
		self.assertEqual(seen, [("Material Request", False), ("Purchase Order", False)])

	def test_budget_guard_blocks_late_purchase_order_lock_and_is_poisoned(self):
		acquire_budget_guards([self.key])
		with self.assertRaises(IoneApplicationError):
			self.read()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def linked(self, rows, orders, *, discovery_effect=None, native_effect=None, scope_effect=None):
		calls = []

		def native(doctype, name, fields, *, lock=False):
			calls.append((doctype, name, lock))
			if native_effect:
				return native_effect(doctype, name, lock)
			return SimpleNamespace(name=name, company=self.source.company, hospital=self.source.hospital)

		def discover(request, line):
			return discovery_effect() if discovery_effect else rows

		def read_order(order, line, source, actor):
			return {(item.order, item.line): item for item in orders}[(order, line)]

		with (
			patch.object(adapter, "native_document", side_effect=native),
			patch.object(adapter, "_discover_order_rows", side_effect=discover),
			patch.object(adapter, "locked_material_request_source", return_value=self.source),
			patch.object(adapter, "_read_order", side_effect=read_order),
			patch.object(adapter, "require_roles"),
			patch.object(adapter, "require_scope", side_effect=scope_effect),
			patch("medops.hrp_budget.services.release_execution.refresh_financial_authority"),
			patch.object(frappe.db, "sql", return_value=[("READ-COMMITTED", 0)]),
		):
			result = adapter.locked_linked_purchase_orders(
				self.source.request,
				self.source.line,
				self.source.organization_unit,
				expected_request_digest=self.source.digest(),
			)
		return result, calls

	def test_linked_orders_lock_all_parents_then_recheck_and_sum_submitted_lines(self):
		(_, original), _ = self.read()
		first = replace(original, order="order-a", line="line-a", quantity="2.000000000")
		second = replace(original, order="order-b", line="line-b", quantity="3.000000000")
		rows = (("order-a", "line-a", 1), ("order-b", "line-b", 1), ("order-c", "line-c", 0))
		(result_source, orders), calls = self.linked(rows, [first, second])
		self.assertEqual((result_source, orders), (self.source, (first, second)))
		self.assertEqual(
			calls[:2],
			[
				("Material Request", "request-a", False),
				("HRP Organization Unit", self.source.organization_unit, False),
			],
		)
		self.assertEqual(
			calls[2:],
			[
				("Material Request", "request-a", False),
				("Purchase Order", "order-a", False),
				("Purchase Order", "order-b", False),
				("Purchase Order", "order-c", False),
				("Material Request", "request-a", True),
				("Purchase Order", "order-a", True),
				("Purchase Order", "order-b", True),
				("Purchase Order", "order-c", True),
			],
		)

	def test_split_order_quantity_cannot_exceed_material_request(self):
		(_, original), _ = self.read()
		first = replace(original, order="order-a", line="line-a", quantity="2.000000000")
		second = replace(original, order="order-b", line="line-b", quantity="4.000000000")
		with self.assertRaises(IoneApplicationError):
			self.linked((("order-a", "line-a", 1), ("order-b", "line-b", 1)), [first, second])

	def test_new_link_after_parent_locks_forces_full_retry(self):
		first = (("order-a", "line-a", 1),)
		changed = (*first, ("order-b", "line-b", 1))
		values = iter([first, changed])
		with self.assertRaises(IoneApplicationError):
			self.linked(first, [], discovery_effect=lambda: next(values))

	def test_linked_order_read_permission_is_required_before_any_parent_lock(self):
		seen = []

		def deny(doctype, name, lock):
			seen.append((doctype, name, lock))
			if doctype == "Purchase Order":
				raise PermissionError("synthetic denial")
			return SimpleNamespace(name=name, company=self.source.company, hospital=self.source.hospital)

		with self.assertRaises(PermissionError):
			self.linked((("order-a", "line-a", 1),), [], native_effect=deny)
		self.assertTrue(all(not lock for _, _, lock in seen))

	def test_unit_scope_denial_happens_before_order_discovery(self):
		def deny(*args, **kwargs):
			raise PermissionError("synthetic scope denial")

		def discovery():
			self.fail("source discovery ran before scope check")

		with self.assertRaises(PermissionError):
			self.linked((), [], discovery_effect=discovery, scope_effect=deny)

	def test_no_linked_orders_produces_empty_observation(self):
		(source, orders), _ = self.linked((), [])
		self.assertEqual((source, orders), (self.source, ()))

	def test_linked_order_locks_after_budget_guard_are_denied(self):
		acquire_budget_guards([self.key])
		with self.assertRaises(IoneApplicationError):
			self.linked((), [])
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])


class TestBudgetPurchaseOrderDiscovery(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.rollback()
		frappe.db.sql(
			"CREATE TEMPORARY TABLE `tabPurchase Order` "
			"(`name` VARCHAR(140) PRIMARY KEY, `docstatus` INT) ENGINE=InnoDB "
			"DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
		)
		frappe.db.sql(
			"CREATE TEMPORARY TABLE `tabPurchase Order Item` "
			"(`name` VARCHAR(140), `parent` VARCHAR(140), `parenttype` VARCHAR(140), "
			"`parentfield` VARCHAR(140), `material_request` VARCHAR(140), "
			"`material_request_item` VARCHAR(140), `docstatus` INT) ENGINE=InnoDB "
			"DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
		)

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.sql("DROP TEMPORARY TABLE IF EXISTS `tabPurchase Order Item`")
		frappe.db.sql("DROP TEMPORARY TABLE IF EXISTS `tabPurchase Order`")
		frappe.db.rollback()
		frappe.db.begin()
		super().tearDown()

	def add(self, order, line, status, *, request="request-a", request_line="request-line"):
		if status is not None:
			frappe.db.sql(
				"INSERT INTO `tabPurchase Order` (`name`, `docstatus`) VALUES (%s, %s)", (order, status)
			)
		frappe.db.sql(
			"""INSERT INTO `tabPurchase Order Item`
			(`name`, `parent`, `parenttype`, `parentfield`, `material_request`, `material_request_item`, `docstatus`)
			VALUES (%s, %s, 'Purchase Order', 'items', %s, %s, %s)""",
			(line, order, request, request_line, status if status is not None else 0),
		)

	def test_finds_all_submitted_draft_and_cancelled_links(self):
		self.add("order-c", "line-c", 2)
		self.add("order-a", "line-a", 1)
		self.add("order-b", "line-b", 0)
		self.add("other", "other-line", 1, request_line="another-line")
		self.assertEqual(
			vars(adapter)["_discover_order_rows"]("request-a", "request-line"),
			(("order-a", "line-a", 1), ("order-b", "line-b", 0), ("order-c", "line-c", 2)),
		)

	def test_missing_parent_and_mismatched_request_are_rejected(self):
		self.add("missing", "line-a", None)
		with self.assertRaises(ValueError):
			vars(adapter)["_discover_order_rows"]("request-a", "request-line")
		frappe.db.sql("DELETE FROM `tabPurchase Order Item`")
		self.add("order-b", "line-b", 1, request="different")
		with self.assertRaises(ValueError):
			vars(adapter)["_discover_order_rows"]("request-a", "request-line")

	def test_overflow_does_not_return_partial_links(self):
		self.add("order-a", "line-a", 1)
		self.add("order-b", "line-b", 1)
		with patch.object(adapter, "MAX_LINKED_ORDER_LINES", 1):
			with self.assertRaises(ValueError):
				vars(adapter)["_discover_order_rows"]("request-a", "request-line")

	def test_collation_alias_is_rejected(self):
		self.add("order-a", "line-a", 1, request_line="REQUEST-LINE")
		with self.assertRaises(ValueError):
			vars(adapter)["_discover_order_rows"]("request-a", "request-line")

	def test_child_and_parent_document_status_must_match(self):
		self.add("order-a", "line-a", 1)
		frappe.db.sql("UPDATE `tabPurchase Order Item` SET `docstatus`=0 WHERE `name`=%s", ("line-a",))
		with self.assertRaises(ValueError):
			vars(adapter)["_discover_order_rows"]("request-a", "request-line")

	def test_missing_lookup_index_fails_before_source_rows(self):
		sql = frappe.db.sql

		def without_index(query, *args, **kwargs):
			if "information_schema.STATISTICS" in query:
				return []
			return sql(query, *args, **kwargs)

		with patch.object(frappe.db, "sql", side_effect=without_index):
			with self.assertRaises(IoneApplicationError):
				vars(adapter)["_discover_order_rows"]("request-a", "request-line")
