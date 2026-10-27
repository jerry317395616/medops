"""Exercise the candidate coordinator with a real Frappe transaction guard."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.budget_control import budget_control_key
from medops.common.budget_control_history import ReleaseInventoryEntry
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services import material_budget_candidates as candidates
from medops.hrp_budget.services import material_budget_guards as material_guards
from medops.hrp_budget.services.control_guards import assert_budget_guards
from medops.hrp_budget.services.material_request_source import MaterialRequestSource
from medops.tests.test_budget_control_sources import input_key


class TestBudgetMaterialCandidates(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		self.values = input_key(company="company-" + uuid4().hex)
		self.key = budget_control_key(self.values)
		self.source = MaterialRequestSource(
			request="request",
			line="line",
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
			docstatus=0,
			status="Draft",
			item_code="item",
			uom="unit",
			expense_account="account",
			project=None,
			quantity="1.000000000",
			rate="10.000000000",
			amount="10.000000000",
		)
		self.entry = ReleaseInventoryEntry("entry", self.key, "CNY", 2, "100.00", "account", "center")
		reader = patch.object(material_guards, "locked_material_request_source", return_value=self.source)
		reader.start()
		self.addCleanup(reader.stop)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.begin()
		super().tearDown()

	def prepare(self):
		return candidates.prepare_purchase_release_match(
			"request",
			"line",
			"unit",
			expected_source_digest=self.source.digest(),
			budget_date="2026-01-15",
			proposed_keys=[self.values],
		)

	def test_guarded_matching_rechecks_sources_and_does_not_write_budget(self):
		with (
			patch.object(candidates, "locked_material_request_source", return_value=self.source) as source,
			patch.object(candidates, "resolve_budget_control_key", side_effect=budget_control_key) as resolve,
			patch.object(candidates, "guarded_release_inventory", return_value=(self.entry,)),
		):
			result = self.prepare()
		self.assertEqual(source.call_count, 3)
		self.assertEqual(resolve.call_count, 3)
		self.assertEqual([candidate.key for candidate in result.candidates], [self.key])
		self.assertEqual(result.source, self.source)
		assert_budget_guards([self.key])

	def test_source_change_poison_guard_and_requires_full_rollback(self):
		changed = replace(self.source, amount="11.000000000")
		with (
			patch.object(candidates, "locked_material_request_source", side_effect=[self.source, changed]),
			patch.object(candidates, "resolve_budget_control_key", side_effect=budget_control_key),
		):
			with self.assertRaises(IoneApplicationError):
				self.prepare()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])
		frappe.db.rollback()
		self.assertIsNone(getattr(frappe.local.db, "_medops_budget_guard_marker", None))

	def test_source_change_during_inventory_poison_guard(self):
		changed = replace(self.source, amount="11.000000000")
		with (
			patch.object(
				candidates, "locked_material_request_source", side_effect=[self.source, self.source, changed]
			),
			patch.object(candidates, "resolve_budget_control_key", side_effect=budget_control_key),
			patch.object(candidates, "guarded_release_inventory", return_value=(self.entry,)),
		):
			with self.assertRaises(IoneApplicationError):
				self.prepare()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_source_permission_revoked_during_inventory_poison_guard(self):
		calls = 0

		def read_source(*_args):
			nonlocal calls
			calls += 1
			if calls == 3:
				candidates.raise_ione_error("PERMISSION_DENIED")
			return self.source

		with (
			patch.object(candidates, "locked_material_request_source", side_effect=read_source),
			patch.object(candidates, "resolve_budget_control_key", side_effect=budget_control_key),
			patch.object(candidates, "guarded_release_inventory", return_value=(self.entry,)),
		):
			with self.assertRaises(IoneApplicationError):
				self.prepare()
		self.assertEqual(calls, 3)
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_key_identity_change_poison_guard(self):
		other = budget_control_key(input_key(company="other-company"))
		with (
			patch.object(candidates, "locked_material_request_source", return_value=self.source),
			patch.object(candidates, "resolve_budget_control_key", side_effect=[self.key, other]),
		):
			with self.assertRaises(IoneApplicationError):
				self.prepare()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_different_unit_is_rejected_before_guard_acquisition(self):
		self.values["organization_unit"] = "other-unit"
		with (
			patch.object(candidates, "locked_material_request_source", return_value=self.source),
			patch.object(candidates, "resolve_budget_control_key", side_effect=budget_control_key),
			patch.object(candidates, "acquire_material_budget_guards") as acquire,
		):
			with self.assertRaises(IoneApplicationError):
				self.prepare()
		acquire.assert_not_called()
