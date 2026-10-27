"""Orchestration regressions using an actual transaction guard and synthetic facts."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.budget_control import budget_control_key
from medops.common.budget_control_history import ReleaseInventoryEntry
from medops.common.budget_material_match import PurchaseBudgetCandidate
from medops.common.error_catalog import IoneApplicationError
from medops.common.material_source_snapshot import material_source_snapshot
from medops.common.reservation_authorization import reservation_intent
from medops.hrp_budget.services import material_usage_preview as preview
from medops.hrp_budget.services import reservation_confirmation
from medops.hrp_budget.services.control_guards import (
	acquire_multi_source_bound_budget_guards,
	assert_budget_guards,
)
from medops.hrp_budget.services.material_budget_guards import MaterialSourceReference
from medops.hrp_budget.services.material_request_source import MaterialRequestSource
from medops.tests.test_budget_control_sources import input_key


class TestBudgetUsagePreview(IntegrationTestCase):
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
			rate="6.000000000",
			amount="6.000000000",
		)
		self.second = replace(self.first, request="request-b", line="line-b")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.begin()
		super().tearDown()

	def ref(self, source):
		return MaterialSourceReference(source.request, source.line, source.organization_unit, source.digest())

	def run_inspection(self, *, discovered=None, verify_side_effect=None, inventory_side_effect=None):
		stages = []
		discovered = (self.ref(self.first),) if discovered is None else discovered

		def discover(keys):
			stages.append("discover")
			return discovered

		def acquire(references, keys):
			stages.append("lock")
			sources_by_id = {(s.request, s.line, s.organization_unit): s for s in (self.first, self.second)}
			sources = tuple(sources_by_id[(r.request, r.line, r.organization_unit)] for r in references)
			bindings = tuple(sorted((s.request, s.line, s.organization_unit, s.digest()) for s in sources))
			acquire_multi_source_bound_budget_guards(keys, source_bindings=bindings)
			return sources

		def verify(keys, sources, *, discovered):
			stages.append("usage")
			if verify_side_effect:
				return verify_side_effect()
			return ()

		def inventory(keys):
			stages.append("releases")
			if inventory_side_effect:
				return inventory_side_effect()
			return ()

		with ExitStack() as stack:
			stack.enter_context(patch.object(preview, "resolve_budget_control_key", return_value=self.key))
			stack.enter_context(
				patch.object(preview, "discover_material_usage_sources", side_effect=discover)
			)
			stack.enter_context(
				patch.object(preview, "acquire_material_budget_guards_for_sources", side_effect=acquire)
			)
			stack.enter_context(
				patch.object(preview, "guarded_verify_material_usage_sources", side_effect=verify)
			)
			stack.enter_context(patch.object(preview, "guarded_release_inventory", side_effect=inventory))
			result = preview.inspect_purchase_usage(
				self.second.request,
				self.second.line,
				self.second.organization_unit,
				expected_source_digest=self.second.digest(),
				budget_date="2026-01-15",
				proposed_keys=[asdict(self.key)],
			)
		return result, stages

	def confirmation_intent(self):
		snapshot, digest = material_source_snapshot(asdict(self.first))
		return reservation_intent(
			source_snapshot=snapshot,
			source_snapshot_digest=digest,
			allocation="test-allocation",
			key=self.key,
			currency="CNY",
			precision=2,
			amount="6.00",
			budget_date="2026-01-15",
			reason="Synthetic test",
		)

	def guarded_confirmation_inspection(self, *args, **kwargs):
		del args, kwargs
		binding = (self.first.request, self.first.line, self.first.organization_unit, self.first.digest())
		acquire_multi_source_bound_budget_guards([self.key], source_bindings=(binding,))
		return preview.PurchaseUsageInspection(
			self.first, "2026-01-15", (PurchaseBudgetCandidate(self.key, "CNY", 2),), ()
		)

	def test_reservation_inspection_rechecks_guard_source_and_scope(self):
		item = self.confirmation_intent()
		with (
			patch.object(reservation_confirmation, "require_web_session"),
			patch.object(reservation_confirmation, "session_digest", return_value="a" * 64),
			patch.object(reservation_confirmation, "require_roles"),
			patch.object(reservation_confirmation, "require_scope") as scopes,
			patch.object(
				reservation_confirmation,
				"inspect_purchase_usage",
				side_effect=self.guarded_confirmation_inspection,
			),
			patch.object(reservation_confirmation, "locked_material_request_source", return_value=self.first),
			patch.object(reservation_confirmation, "_append", return_value=SimpleNamespace(name="synthetic")),
		):
			result = reservation_confirmation.prepare_reservation_confirmation(item)
			self.assertEqual(result["confirmation"], "synthetic")
			self.assertGreaterEqual(scopes.call_count, 6)
			assert_budget_guards([self.key])

	def test_reservation_prepare_calls_real_purchase_inspector_with_canonical_key(self):
		item = self.confirmation_intent()
		entry = ReleaseInventoryEntry("release-row", self.key, "CNY", 2, "10.00", "account", "center")

		def acquire(references, keys):
			self.assertEqual(references, (self.ref(self.first),))
			binding = (self.first.request, self.first.line, self.first.organization_unit, self.first.digest())
			acquire_multi_source_bound_budget_guards(keys, source_bindings=(binding,))
			return (self.first,)

		with (
			patch.object(reservation_confirmation, "require_web_session"),
			patch.object(reservation_confirmation, "session_digest", return_value="a" * 64),
			patch.object(reservation_confirmation, "require_roles"),
			patch.object(reservation_confirmation, "require_scope"),
			patch.object(reservation_confirmation, "locked_material_request_source", return_value=self.first),
			patch.object(reservation_confirmation, "_append", return_value=SimpleNamespace(name="synthetic")),
			patch.object(preview, "resolve_budget_control_key", return_value=self.key),
			patch.object(preview, "discover_material_usage_sources", return_value=()),
			patch.object(preview, "acquire_material_budget_guards_for_sources", side_effect=acquire),
			patch.object(preview, "guarded_verify_material_usage_sources", return_value=()),
			patch.object(preview, "guarded_release_inventory", return_value=(entry,)),
		):
			result = reservation_confirmation.prepare_reservation_confirmation(item)
			self.assertEqual(result["confirmation"], "synthetic")
			assert_budget_guards([self.key])

	def test_reservation_confirmation_rejects_session_change_and_invalidates_guard(self):
		item = self.confirmation_intent()
		with (
			patch.object(reservation_confirmation, "require_web_session"),
			patch.object(
				reservation_confirmation, "session_digest", side_effect=["a" * 64, "a" * 64, "b" * 64]
			),
			patch.object(reservation_confirmation, "require_roles"),
			patch.object(reservation_confirmation, "require_scope"),
			patch.object(
				reservation_confirmation,
				"inspect_purchase_usage",
				side_effect=self.guarded_confirmation_inspection,
			),
			patch.object(reservation_confirmation, "locked_material_request_source", return_value=self.first),
			patch.object(reservation_confirmation, "_append") as append,
		):
			with self.assertRaises(IoneApplicationError):
				reservation_confirmation.prepare_reservation_confirmation(item)
			append.assert_not_called()
			with self.assertRaises(IoneApplicationError):
				assert_budget_guards([self.key])

	def test_historical_and_proposed_sources_are_checked_before_and_after_releases(self):
		result, stages = self.run_inspection()
		self.assertEqual(stages, ["discover", "lock", "usage", "releases", "usage"])
		self.assertEqual((result.source, result.candidates, result.usage), (self.second, (), ()))
		assert_budget_guards([self.key])

	def test_first_source_does_not_require_a_historical_reservation(self):
		result, stages = self.run_inspection(discovered=())
		self.assertEqual(result.source, self.second)
		self.assertEqual(stages, ["discover", "lock", "usage", "releases", "usage"])

	def test_matching_release_is_returned_as_candidate_not_a_balance(self):
		entry = ReleaseInventoryEntry("release-row", self.key, "CNY", 2, "10.00", "account", "center")
		result, _ = self.run_inspection(inventory_side_effect=lambda: (entry,))
		self.assertEqual(
			[(c.key, c.currency, c.precision) for c in result.candidates], [(self.key, "CNY", 2)]
		)
		self.assertEqual(result.usage, ())

	def test_currency_incompatible_release_invalidates_guard(self):
		entry = ReleaseInventoryEntry("release-row", self.key, "USD", 2, "10.00", "account", "center")
		with self.assertRaises(IoneApplicationError):
			self.run_inspection(inventory_side_effect=lambda: (entry,))
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_historical_snapshot_conflict_rejected_before_guard(self):
		changed = replace(self.second, amount="7.000000000")
		with self.assertRaises(IoneApplicationError):
			self.run_inspection(discovered=(self.ref(changed),))
		self.assertIsNone(getattr(frappe.local.db, "_medops_budget_guard_marker", None))

	def test_late_usage_change_invalidates_guard(self):
		values = iter([(), ("changed",)])
		with self.assertRaises(IoneApplicationError):
			self.run_inspection(verify_side_effect=lambda: next(values))
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_release_read_failure_invalidates_guard(self):
		def deny():
			raise PermissionError("synthetic denial")

		with self.assertRaises(PermissionError):
			self.run_inspection(inventory_side_effect=deny)
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_actor_change_during_release_inspection_invalidates_guard(self):
		def change_actor():
			frappe.set_user("Guest")
			return ()

		with self.assertRaises(IoneApplicationError):
			self.run_inspection(inventory_side_effect=change_actor)
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])
