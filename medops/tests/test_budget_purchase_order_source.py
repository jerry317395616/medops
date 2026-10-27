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
