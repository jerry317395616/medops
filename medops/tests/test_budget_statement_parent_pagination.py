"""Batch provenance enumeration must page completely and fail closed."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services import budget_statement as statements


class TestBudgetStatementParentPagination(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.context = dict(
			company="company",
			hospital="hospital",
			organization_version="version",
			organization_unit="unit",
			fiscal_year="year",
			currency="CNY",
			fiscal_year_start="2026-01-01",
			to_date="2026-12-31",
		)

	def test_all_posted_batches_are_enumerated_beyond_previous_ceiling(self):
		names = [f"release-{index:05}" for index in range(1001)]
		calls = []

		def get_all(doctype, *, filters, pluck, order_by, limit_page_length):
			self.assertEqual(doctype, statements.RELEASE)
			self.assertEqual((pluck, order_by), ("name", "name asc"))
			self.assertEqual(filters["posting_date"], ["between", ["2026-01-01", "2026-12-31"]])
			self.assertEqual((filters["company"], filters["hospital"]), ("company", "hospital"))
			self.assertNotIn("organization_unit", filters)
			calls.append(dict(filters))
			cursor = filters.get("name")
			return [name for name in names if not cursor or name > cursor[1]][:limit_page_length]

		with (
			patch.object(statements, "require_statement_scope") as scope,
			patch.object(frappe, "get_all", side_effect=get_all),
		):
			result = statements.statement_batches(self.context, export=True)
		self.assertEqual(result, tuple(names))
		self.assertEqual(len(calls), 8)
		self.assertEqual(calls[1]["name"], [">", names[127]])
		scope.assert_called_once_with(self.context, export=True)

	def test_duplicate_and_proof_overflow_do_not_return_partial_names(self):
		with (
			patch.object(statements, "require_statement_scope"),
			patch.object(statements, "STATEMENT_PARENT_PAGE_SIZE", 2),
			patch.object(frappe, "get_all", side_effect=[["a", "b"], ["b"]]),
		):
			with self.assertRaises(IoneApplicationError) as duplicate:
				statements.statement_batches(self.context)
			self.assertEqual(duplicate.exception.code, "IONE-CORE-0009")
		with (
			patch.object(statements, "require_statement_scope"),
			patch.object(statements, "STATEMENT_PARENT_PAGE_SIZE", 2),
			patch.object(statements, "MAX_PRIVATE_PROOF_ENTRIES", 2),
			patch.object(frappe, "get_all", side_effect=[["a", "b"], ["c"]]),
		):
			with self.assertRaises(IoneApplicationError) as overflow:
				statements.statement_batches(self.context)
			self.assertEqual(overflow.exception.code, "IONE-CORE-0003")
