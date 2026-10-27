"""Bounded multi-source ordering and transaction witness regressions."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.budget_control import budget_control_key
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services import material_budget_guards as material_guards
from medops.hrp_budget.services.control_guards import assert_budget_guards, require_material_source_lock_order
from medops.hrp_budget.services.material_request_source import MaterialRequestSource
from medops.tests.test_budget_control_sources import input_key


class TestBudgetMultiSourceGuards(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		self.key = budget_control_key(input_key(company="company-" + uuid4().hex))
		self.first = MaterialRequestSource(
			request="request-a",
			line="line-a",
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
			quantity="1.000000000",
			rate="10.000000000",
			amount="10.000000000",
		)
		self.second = replace(self.first, request="request-b", line="line-b")
		self.references = [self.reference(self.first), self.reference(self.second)]

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.begin()
		super().tearDown()

	def reference(self, source):
		return material_guards.MaterialSourceReference(
			source.request, source.line, source.organization_unit, source.digest()
		)

	def acquire(self, *, source_reads=None, native_reads=None, references=None, keys=None):
		calls = []

		def native(doctype, name, fields, *, lock=False):
			calls.append((name, lock))
			if native_reads:
				return native_reads(name, lock)
			return SimpleNamespace(name=name)

		def source(request, line, unit):
			if source_reads:
				return source_reads(request, line, unit)
			return {self.first.request: self.first, self.second.request: self.second}[request]

		with (
			patch.object(material_guards, "native_document", side_effect=native),
			patch.object(material_guards, "locked_material_request_source", side_effect=source),
		):
			result = material_guards.acquire_material_budget_guards_for_sources(
				references if references is not None else list(reversed(self.references)),
				keys if keys is not None else [self.key],
			)
		return result, calls

	def test_all_parents_prelock_in_order_before_sources_and_bindings(self):
		stages = []

		def native(name, lock):
			stages.append(("parent", name, lock))
			return SimpleNamespace(name=name)

		def source(request, line, unit):
			stages.append(("source", request))
			return {self.first.request: self.first, self.second.request: self.second}[request]

		sources, calls = self.acquire(native_reads=native, source_reads=source)
		self.assertEqual(sources, (self.first, self.second))
		self.assertEqual(
			calls, [("request-a", False), ("request-b", False), ("request-a", True), ("request-b", True)]
		)
		self.assertEqual(stages[:4], [("parent", name, lock) for name, lock in calls])
		self.assertEqual(stages[4:], [("source", "request-a"), ("source", "request-b")] * 2)
		material_guards.assert_material_budget_guards(self.first, [self.key])
		material_guards.assert_material_budget_guards(self.second, [self.key])
		require_material_source_lock_order(
			self.second.request, self.second.line, self.second.organization_unit
		)

	def test_changed_source_after_guard_poisons_transaction(self):
		counts = {self.first.request: 0, self.second.request: 0}

		def source(request, line, unit):
			counts[request] += 1
			value = {self.first.request: self.first, self.second.request: self.second}[request]
			if request == self.second.request and counts[request] == 2:
				frappe.db.savepoint("multi_after_guard")
				return replace(value, amount="11.000000000")
			return value

		with self.assertRaises(IoneApplicationError):
			self.acquire(source_reads=source)
		frappe.db.rollback(save_point="multi_after_guard")
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_unlisted_source_cannot_be_locked_after_guard(self):
		self.acquire()
		with self.assertRaises(IoneApplicationError):
			require_material_source_lock_order("request-c", "line-c", self.key.organization_unit)
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_wrong_source_snapshot_rejected_under_guard(self):
		self.acquire()
		with self.assertRaises(IoneApplicationError):
			material_guards.assert_material_budget_guards(
				replace(self.first, amount="11.000000000"), [self.key]
			)
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_duplicate_reference_and_scope_mismatch_are_rejected(self):
		with self.assertRaises(ValueError):
			self.acquire(references=[self.references[0], self.references[0]])
		with self.assertRaises(ValueError):
			self.acquire(keys=[replace(self.key, project="another-project")])
		self.assertIsNone(getattr(frappe.local.db, "_medops_budget_guard_marker", None))

	def test_source_permission_failure_prevents_guard(self):
		def deny(name, lock):
			if name == "request-b":
				raise PermissionError("synthetic source denial")
			return SimpleNamespace(name=name)

		with self.assertRaises(PermissionError):
			self.acquire(native_reads=deny)
		self.assertIsNone(getattr(frappe.local.db, "_medops_budget_guard_marker", None))
