"""Cross-source usage inspection on connection-local synthetic tables."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.budget_control import budget_control_key
from medops.common.budget_reservation_anchor import (
	ANCHOR_ROW_FIELDS,
	reservation_anchor,
	reservation_anchor_row,
)
from medops.common.budget_reservation_events import ReservationEvent
from medops.common.budget_reservation_proof import (
	ReservationSource,
	reservation_event_key,
	reservation_record,
)
from medops.common.budget_reservation_storage import ROW_FIELDS, reservation_record_row
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services import budget_usage_observation as observation
from medops.hrp_budget.services.control_guards import acquire_budget_guards, assert_budget_guards
from medops.tests.test_budget_control_sources import input_key


class TestBudgetUsageObservation(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for table, fields in (
			("tabHRP Budget Reservation", ANCHOR_ROW_FIELDS),
			("tabHRP Budget Reservation Event", ROW_FIELDS),
		):
			columns = []
			for field in sorted(fields):
				if field in {"event_count", "sequence", "currency_precision", "proof_version"}:
					kind = "INT"
				elif field in {"period_start", "period_end"}:
					kind = "DATE"
				elif field == "occurred_at":
					kind = "DATETIME(6)"
				else:
					kind = "VARCHAR(255)"
				columns.append(f"`{field}` {kind}")
			frappe.db.sql(
				f"CREATE TEMPORARY TABLE `{table}` ({', '.join(columns)}) ENGINE=InnoDB "
				"DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"  # nosec B608 - fixed registry and fields
			)
			frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		self.key = budget_control_key(input_key(company="company-" + uuid4().hex))
		acquire_budget_guards([self.key])

	def tearDown(self):
		frappe.db.rollback()
		for table in ("tabHRP Budget Reservation Event", "tabHRP Budget Reservation"):
			frappe.db.sql(f"DROP TEMPORARY TABLE IF EXISTS `{table}`")  # nosec B608 - fixed registry
			frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.begin()
		super().tearDown()

	def evidence(self, suffix, *, key=None, amount="6.00", commit=None):
		key = key or self.key
		name = "reservation-" + suffix
		source = ReservationSource(
			"Material Request", "request-" + suffix, "a" * 64, "line", "allocation", "Reserve"
		)
		first = reservation_record(
			ReservationEvent(reservation_event_key(name, 1), name, 1, key, "CNY", 2, "Reserve", amount),
			source,
			actor="Administrator",
			occurred_at="2026-10-27 09:30:00.000000",
			previous_digest=None,
		)
		records = [first]
		if commit:
			records.append(
				reservation_record(
					ReservationEvent(
						reservation_event_key(name, 2), name, 2, key, "CNY", 2, "Commit", commit
					),
					replace(source, action="Commit"),
					actor="Administrator",
					occurred_at="2026-10-27 09:31:00.000000",
					previous_digest=first.event_digest,
				)
			)
		anchor = reservation_anchor(
			records, expected_source=source, source_snapshot_digest="a" * 64, cost_center="center"
		)
		return reservation_anchor_row(anchor), [reservation_record_row(record) for record in records]

	def insert(self, table, row):
		fields = sorted(row)
		columns = ", ".join(f"`{name}`" for name in fields)
		placeholders = ", ".join(["%s"] * len(fields))
		frappe.db.sql(
			f"INSERT INTO `{table}` ({columns}) VALUES ({placeholders})",  # nosec B608 - test-only tables and fixed fields
			tuple(row[field] for field in fields),
		)

	def add_evidence(self, root, events):
		self.insert("tabHRP Budget Reservation", root)
		for event in events:
			self.insert("tabHRP Budget Reservation Event", event)

	def observe(self):
		with patch.object(observation, "_access"):
			return observation.guarded_budget_usage_observation([self.key])

	def test_distinct_sources_and_state_transfers_are_not_double_counted(self):
		self.add_evidence(*self.evidence("a", amount="6.00", commit="4.00"))
		self.add_evidence(*self.evidence("b", amount="5.00"))
		positions = self.observe()
		self.assertEqual(
			[
				(item.reservation, item.open_reservation, item.open_commitment, item.actual)
				for item in positions
			],
			[("reservation-a", "2.00", "4.00", "0.00"), ("reservation-b", "5.00", "0.00", "0.00")],
		)
		assert_budget_guards([self.key])

	def test_orphan_event_invalidates_the_guard(self):
		_, events = self.evidence("a")
		self.insert("tabHRP Budget Reservation Event", events[0])
		with self.assertRaises(IoneApplicationError):
			self.observe()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_event_with_changed_scope_is_found_by_parent_and_rejected(self):
		root, events = self.evidence("a")
		events[0]["budget_subject"] = "wrong-subject"
		self.add_evidence(root, events)
		with self.assertRaises(IoneApplicationError):
			self.observe()

	def test_event_with_changed_source_allocation_is_rejected(self):
		root, events = self.evidence("a", commit="4.00")
		events[1]["source_allocation"] = "other-allocation"
		self.add_evidence(root, events)
		with self.assertRaises(IoneApplicationError):
			self.observe()

	def test_overflow_is_rejected_without_returning_partial_scope(self):
		self.add_evidence(*self.evidence("a"))
		self.add_evidence(*self.evidence("b"))
		with patch.object(observation, "MAX_USAGE_ROOTS", 1):
			with self.assertRaises(IoneApplicationError):
				self.observe()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_unknown_source_is_rejected(self):
		root, events = self.evidence("a")
		root["source_doctype"] = "Other Source"
		self.add_evidence(root, events)
		with self.assertRaises(IoneApplicationError):
			self.observe()

	def test_duplicate_action_across_chains_is_rejected(self):
		root_a, events_a = self.evidence("a")
		root_b, events_b = self.evidence("b")
		events_b[0]["source_action_key"] = events_a[0]["source_action_key"]
		self.add_evidence(root_a, events_a)
		self.add_evidence(root_b, events_b)
		with self.assertRaises(IoneApplicationError):
			self.observe()

	def test_missing_event_is_rejected(self):
		root, _ = self.evidence("a")
		self.insert("tabHRP Budget Reservation", root)
		with self.assertRaises(IoneApplicationError):
			self.observe()

	def test_same_scope_other_period_is_not_silently_omitted(self):
		other = replace(self.key, period_start="2026-02-01", period_end="2026-02-28")
		self.add_evidence(*self.evidence("a", key=other))
		positions = self.observe()
		self.assertEqual((len(positions), positions[0].key), (1, other))

	def test_other_organization_is_not_exposed(self):
		other = replace(self.key, organization_unit="another-unit")
		self.add_evidence(*self.evidence("a", key=other))
		self.assertEqual(self.observe(), ())

	def test_guest_cannot_inspect_usage(self):
		frappe.set_user("Guest")
		with patch.object(observation, "require_valid_usage_storage") as storage:
			with self.assertRaises(IoneApplicationError):
				observation.guarded_budget_usage_observation([self.key])
			storage.assert_not_called()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_late_access_denial_invalidates_the_guard(self):
		with patch.object(observation, "_access", side_effect=[None, PermissionError("synthetic denial")]):
			with self.assertRaises(PermissionError):
				observation.guarded_budget_usage_observation([self.key])
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])
