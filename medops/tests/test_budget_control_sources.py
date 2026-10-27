from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.budget_control import budget_control_key, budget_guard_names
from medops.common.budget_ledger import BudgetReleaseFacts
from medops.hrp_budget.services.control_sources import (
	resolve_budget_control_key,
	resolve_release_control_keys,
)
from medops.hrp_budget.services.planning import native_document


def input_key(**changes):
	return {
		"company": "company",
		"hospital": "hospital",
		"organization_unit": "unit",
		"fiscal_year": "fy26",
		"budget_subject": "subject",
		"funding_source": "fund",
		"project": None,
		"period_start": "2026-01-01",
		"period_end": "2026-01-31",
		**changes,
	}


class TestBudgetControlSources(IntegrationTestCase):
	def test_resolves_aliases_to_persisted_names_before_guard_identity(self):
		calls = []

		def lookup(doctype, name, fields):
			calls.append((doctype, name, fields))
			return SimpleNamespace(name=name.upper())

		with patch("medops.hrp_budget.services.control_sources.native_document", side_effect=lookup):
			first = resolve_budget_control_key(input_key())
			second = resolve_budget_control_key(
				input_key(
					company="COMPANY",
					hospital="HOSPITAL",
					organization_unit="UNIT",
					fiscal_year="FY26",
					budget_subject="SUBJECT",
					funding_source="FUND",
				)
			)
		self.assertEqual(first, second)
		self.assertEqual(budget_guard_names([first]), budget_guard_names([second]))
		self.assertEqual(len(calls), 12)
		self.assertTrue(all(fields == {"name"} for _, _, fields in calls))
		self.assertNotIn("Project", [doctype for doctype, _, _ in calls])

	def test_optional_project_is_resolved_and_reader_denials_are_not_swallowed(self):
		def lookup(doctype, name, fields):
			if doctype == "Project":
				raise PermissionError("synthetic source denial")
			return SimpleNamespace(name=name)

		with patch("medops.hrp_budget.services.control_sources.native_document", side_effect=lookup):
			with self.assertRaises(PermissionError):
				resolve_budget_control_key(input_key(project="project"))

	def test_standard_reader_returns_persisted_name_for_actual_database_alias(self):
		canonical = frappe.db.get_value("User", "administrator", "name")
		if canonical is None:
			self.skipTest("site collation does not resolve this case alias")
		self.assertEqual(canonical, "Administrator")
		previous_user = frappe.session.user
		try:
			frappe.set_user("Administrator")
			self.assertEqual(native_document("User", "administrator", {"name"}).name, canonical)
		finally:
			frappe.set_user(previous_user)

	def test_batch_resolves_and_deduplicates_aliases_with_a_call_local_cache(self):
		first = budget_control_key(input_key())
		alias = budget_control_key(input_key(organization_unit="UNIT"))
		fetches = []

		def lookup(doctype, name, fields, *, cache):
			key = (doctype, name)
			if key not in cache:
				fetches.append(key)
				cache[key] = SimpleNamespace(name=name.upper())
			return cache[key]

		with (
			patch(
				"medops.hrp_budget.services.control_sources.budget_keys_for_release_batch",
				return_value=(first, alias),
			),
			patch("medops.hrp_budget.services.control_sources.native_document", side_effect=lookup),
		):
			resolved = resolve_release_control_keys(cast(BudgetReleaseFacts, object()))
			self.assertEqual(len(resolved), 1)
			self.assertEqual(resolved[0].organization_unit, "UNIT")
			self.assertEqual(len(fetches), 7)
			resolve_release_control_keys(cast(BudgetReleaseFacts, object()))
			self.assertEqual(len(fetches), 14, "cache must not cross separate calls")

	def test_batch_propagates_read_denial_before_returning_any_keys(self):
		first = budget_control_key(input_key())
		second = budget_control_key(input_key(organization_unit="denied"))

		def lookup(doctype, name, fields, *, cache):
			if doctype == "HRP Organization Unit" and name == "denied":
				raise PermissionError("synthetic source denial")
			return SimpleNamespace(name=name)

		with (
			patch(
				"medops.hrp_budget.services.control_sources.budget_keys_for_release_batch",
				return_value=(first, second),
			),
			patch("medops.hrp_budget.services.control_sources.native_document", side_effect=lookup),
		):
			with self.assertRaises(PermissionError):
				resolve_release_control_keys(cast(BudgetReleaseFacts, object()))
