"""Exercise reversal usage predicates against an isolated temporary table."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.budget_control import budget_control_key, ordered_budget_keys
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services import release_usage_guard as usage
from medops.tests.test_budget_control_guards import synthetic_key


class TestBudgetReleaseUsageGuard(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		# The owned test starts with a clean transaction. Frappe conservatively
		# rejects DDL after writes, even for MariaDB connection-local tables.
		frappe.db.rollback()
		self.table = "medops_test_usage_" + uuid4().hex
		self.key = budget_control_key(synthetic_key(uuid4().hex))
		# This is a connection-local temporary table, not an HRP ledger or model.
		frappe.db.sql(
			f"""CREATE TEMPORARY TABLE `{self.table}` (
			`company` VARCHAR(140), `hospital` VARCHAR(140), `organization_unit` VARCHAR(140),
			`budget_subject` VARCHAR(140), `funding_source` VARCHAR(140), `project` VARCHAR(140),
			`period_start` DATE, `period_end` DATE
			) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""  # nosec B608 - generated test identifier
		)
		self.registry = patch.object(usage, "TABLES", (self.table,))
		self.registry.start()
		self.addCleanup(self.registry.stop)

	def tearDown(self):
		# Roll back synthetic rows before the native implicit-commit guard sees
		# DROP. The table belongs only to this test connection.
		frappe.db.rollback()
		frappe.db.sql(f"DROP TEMPORARY TABLE IF EXISTS `{self.table}`")  # nosec B608 - generated test identifier
		super().tearDown()

	def insert(self, key, **changes):
		values = {
			"company": key.company,
			"hospital": key.hospital,
			"organization_unit": key.organization_unit,
			"budget_subject": key.budget_subject,
			"funding_source": key.funding_source,
			"project": key.project,
			"period_start": key.period_start,
			"period_end": key.period_end,
		}
		values.update(changes)
		frappe.db.sql(
			f"""INSERT INTO `{self.table}` (`company`, `hospital`, `organization_unit`,
			`budget_subject`, `funding_source`, `project`, `period_start`, `period_end`)
			VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",  # nosec B608 - generated test identifier
			tuple(values.values()),
		)

	def clear(self):
		frappe.db.sql(f"DELETE FROM `{self.table}`")  # nosec B608 - generated test identifier

	def check(self, keys):
		# The query predicates use a test-only temporary table; the transaction
		# witness and production table engines are covered by other native tests.
		with (
			patch.object(usage, "assert_budget_guards"),
			patch.object(usage, "_table_engines", return_value=()),
		):
			usage.assert_no_unreconciled_reversal_usage(keys)

	def assert_denied(self, keys, code):
		with self.assertRaises(IoneApplicationError) as denied:
			self.check(keys)
		self.assertEqual(denied.exception.code, code)

	def test_overlap_is_inclusive_and_nonoverlap_is_allowed(self):
		self.insert(self.key, period_start="2026-02-01", period_end="2026-02-28")
		self.check([self.key])
		self.clear()
		self.insert(self.key, period_start="2026-01-31", period_end="2026-02-28")
		self.assert_denied([self.key], "IONE-CORE-0006")

	def test_different_unit_project_and_source_do_not_match(self):
		for changes in (
			{"organization_unit": "another-unit"},
			{"budget_subject": "another-subject"},
			{"funding_source": "another-fund"},
			{"project": "another-project"},
		):
			with self.subTest(changes=changes):
				self.clear()
				self.insert(self.key, **changes)
				self.check([self.key])
		self.clear()
		self.insert(self.key, project="")
		self.assert_denied([self.key], "IONE-CORE-0006")

	def test_unscoped_and_malformed_rows_are_detected(self):
		self.insert(self.key, company=None)
		self.assert_denied([self.key], "IONE-CORE-0009")
		self.clear()
		self.insert(self.key, budget_subject="")
		self.assert_denied([self.key], "IONE-CORE-0009")
		self.check([replace(self.key, organization_unit="other")])
		self.clear()
		self.insert(self.key, period_start="2026-02-01", period_end="2026-01-01")
		self.assert_denied([self.key], "IONE-CORE-0009")

	def test_later_query_chunk_is_checked(self):
		keys = ordered_budget_keys(
			[replace(self.key, budget_subject=f"subject-{index:03}") for index in range(101)]
		)
		self.insert(keys[-1])
		self.assert_denied(keys, "IONE-CORE-0006")
		self.check(keys[:-1])

	def test_empty_unadapted_table_is_allowed_but_any_record_blocks_reversal(self):
		with (
			patch.object(usage, "TABLES", ()),
			patch.object(usage, "UNSUPPORTED_TABLES", (self.table,)),
			patch.object(usage, "assert_budget_guards"),
			patch.object(usage, "_table_engines", return_value=(self.table,)),
		):
			usage.assert_no_unreconciled_reversal_usage([self.key])
			self.insert(self.key)
			with self.assertRaises(IoneApplicationError) as denied:
				usage.assert_no_unreconciled_reversal_usage([self.key])
			self.assertEqual(denied.exception.code, "IONE-CORE-0006")
