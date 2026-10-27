"""Bounded traversal of every posted parent without ledger-derived filtering."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.budget_control import budget_control_key
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services import control_inventory
from medops.tests.test_budget_control_guards import synthetic_key


class TestBudgetInventoryPagination(IntegrationTestCase):
	def test_pages_all_posted_parents_beyond_previous_thousand_batch_limit(self):
		first = [f"release-{index:05}" for index in range(1001)]
		second = ["release-other-1", "release-other-2"]
		calls = []

		def get_all(doctype, *, filters, pluck, order_by, limit_page_length):
			self.assertEqual(doctype, control_inventory.RELEASE)
			self.assertEqual((pluck, order_by), ("name", "name asc"))
			self.assertEqual((filters["status"], filters["docstatus"]), ("Posted", 1))
			calls.append(dict(filters))
			values = first if filters["hospital"] == "hospital-a" else second
			cursor = filters.get("name")
			if cursor:
				self.assertEqual(cursor[0], ">")
				values = [name for name in values if name > cursor[1]]
			return values[:limit_page_length]

		units = [("company", "hospital-a", "unit-1"), ("company", "hospital-b", "unit-2")]
		with patch.object(frappe, "get_all", side_effect=get_all):
			result = list(control_inventory.posted_release_names_for_guarded_inventory(units))
		self.assertEqual(result, first + second)
		self.assertEqual(len(calls), 9)
		self.assertNotIn("name", calls[0])
		self.assertEqual(calls[1]["name"], [">", first[127]])
		self.assertNotIn("name", calls[-1])

	def test_duplicate_and_proof_work_overflow_fail_closed(self):
		units = [("company", "hospital", "unit")]
		with (
			patch.object(control_inventory, "RELEASE_PARENT_PAGE_SIZE", 2),
			patch.object(frappe, "get_all", side_effect=[["a", "b"], ["b"]]),
		):
			with self.assertRaises(IoneApplicationError) as duplicate:
				list(control_inventory.posted_release_names_for_guarded_inventory(units))
			self.assertEqual(duplicate.exception.code, "IONE-CORE-0009")
		with (
			patch.object(control_inventory, "RELEASE_PARENT_PAGE_SIZE", 2),
			patch.object(control_inventory, "MAX_PRIVATE_PROOF_ENTRIES", 2),
			patch.object(frappe, "get_all", side_effect=[["a", "b"], ["c"]]),
		):
			with self.assertRaises(IoneApplicationError) as overflow:
				list(control_inventory.posted_release_names_for_guarded_inventory(units))
			self.assertEqual(overflow.exception.code, "IONE-CORE-0003")

	def test_inventory_checks_every_parent_and_rejects_late_invalid_source(self):
		key = budget_control_key(synthetic_key("pagination"))
		names = [f"release-{index:05}" for index in range(1001)]
		facts = SimpleNamespace(
			snapshot=SimpleNamespace(
				context=SimpleNamespace(company=key.company, hospital=key.hospital),
				lines=[SimpleNamespace(organization_unit="other-unit")],
			)
		)
		visited = []
		invalid_last = False

		def get_all(doctype, *, filters, **kwargs):
			if doctype == control_inventory.LEDGER:
				return []
			self.assertEqual(doctype, control_inventory.RELEASE)
			cursor = filters.get("name")
			return [name for name in names if not cursor or name > cursor[1]][: kwargs["limit_page_length"]]

		def read_facts(parent):
			visited.append(parent.name)
			if invalid_last and parent.name == names[-1]:
				raise ValueError("synthetic incomplete source")
			return facts

		with (
			patch.object(frappe, "get_all", side_effect=get_all),
			patch.object(frappe, "get_doc", side_effect=lambda doctype, name: SimpleNamespace(name=name)),
			patch.object(control_inventory, "stored_release_facts", side_effect=read_facts),
			patch.object(control_inventory, "_check_context"),
			patch.object(control_inventory, "assert_budget_guards"),
			patch.object(control_inventory, "invalidate_budget_guards") as invalidated,
			patch("medops.hrp_budget.services.release_execution.refresh_financial_authority"),
		):
			self.assertEqual(control_inventory.guarded_release_inventory([key]), ())
			self.assertEqual(visited, names)
			visited.clear()
			invalid_last = True
			with self.assertRaisesRegex(ValueError, "synthetic incomplete source"):
				control_inventory.guarded_release_inventory([key])
		self.assertEqual(visited, names)
		invalidated.assert_called_once_with()
