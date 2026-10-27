"""Exercise source-level reservation inspection with a real transaction guard."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.budget_control import budget_control_key
from medops.common.budget_reservation_anchor import reservation_anchor, reservation_anchor_row
from medops.common.budget_reservation_events import ReservationEvent
from medops.common.budget_reservation_proof import (
	ReservationSource,
	reservation_event_key,
	reservation_record,
)
from medops.common.budget_reservation_storage import reservation_record_from_row, reservation_record_row
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services import material_budget_guards as material_guards
from medops.hrp_budget.services import material_reservation_observation as observation
from medops.hrp_budget.services.control_guards import acquire_budget_guards, assert_budget_guards
from medops.hrp_budget.services.material_request_source import MaterialRequestSource
from medops.tests.test_budget_control_sources import input_key


class TestBudgetReservationObservation(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		self.key = budget_control_key(input_key(company="company-" + uuid4().hex))
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
		with patch.object(material_guards, "locked_material_request_source", return_value=self.source):
			material_guards.acquire_material_budget_guards(self.source, [self.key])

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.begin()
		super().tearDown()

	def evidence(self, allocation, amount):
		name = "reservation-" + allocation
		digest = self.source.digest()
		origin = ReservationSource("Material Request", "request", digest, "line", allocation, "Reserve")
		event = ReservationEvent(
			reservation_event_key(name, 1), name, 1, self.key, "CNY", 2, "Reserve", amount
		)
		record = reservation_record(
			event,
			origin,
			actor="Administrator",
			occurred_at="2026-10-27 09:30:00.000000",
			previous_digest=None,
		)
		anchor = reservation_anchor(
			[record], expected_source=origin, source_snapshot_digest=digest, cost_center="center"
		)
		return frappe._dict(reservation_anchor_row(anchor)), frappe._dict(reservation_record_row(record))

	def observe(self, roots, events, *, allocations=None, source_reads=None, permission_check=None):
		with ExitStack() as stack:
			stack.enter_context(patch.object(observation, "_check_permissions", side_effect=permission_check))
			stack.enter_context(patch.object(observation, "_check_row"))
			stack.enter_context(patch.object(observation, "_source_reservations", return_value=roots))
			stack.enter_context(patch.object(observation, "_related_events", return_value=events))
			stack.enter_context(
				patch.object(
					observation,
					"locked_material_request_source",
					side_effect=source_reads or [self.source, self.source],
				)
			)
			return observation.guarded_material_reservation_observation(
				self.source,
				[self.key],
				allocations=allocations if allocations is not None else {"a": self.key, "b": self.key},
			)

	def test_multiple_allocations_match_full_source_amount(self):
		root_a, event_a = self.evidence("a", "6.00")
		root_b, event_b = self.evidence("b", "4.00")
		positions = self.observe([root_b, root_a], [event_a, event_b])
		self.assertEqual([position.open_reservation for position in positions], ["6.00", "4.00"])
		assert_budget_guards([self.key])

	def test_combined_allocations_cannot_exceed_source(self):
		root_a, event_a = self.evidence("a", "6.00")
		root_b, event_b = self.evidence("b", "6.00")
		with self.assertRaises(IoneApplicationError):
			self.observe([root_a, root_b], [event_a, event_b])
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_released_amount_does_not_consume_source_twice(self):
		_, event_a = self.evidence("a", "6.00")
		first = reservation_record_from_row(event_a)
		release = ReservationEvent(
			reservation_event_key("reservation-a", 2),
			"reservation-a",
			2,
			self.key,
			"CNY",
			2,
			"Release Reservation",
			"6.00",
		)
		second = reservation_record(
			release,
			replace(first.source, action="Release Reservation"),
			actor="Administrator",
			occurred_at="2026-10-27 09:31:00.000000",
			previous_digest=first.event_digest,
		)
		anchor = reservation_anchor(
			[first, second],
			expected_source=first.source,
			source_snapshot_digest=self.source.digest(),
			cost_center="center",
		)
		root_a = frappe._dict(reservation_anchor_row(anchor))
		root_b, event_b = self.evidence("b", "10.00")
		positions = self.observe(
			[root_a, root_b], [event_b, frappe._dict(reservation_record_row(second)), event_a]
		)
		self.assertEqual([position.open_reservation for position in positions], ["0.00", "10.00"])

	def test_same_allocation_can_reserve_again_after_release(self):
		_, event_row = self.evidence("a", "6.00")
		first = reservation_record_from_row(event_row)
		release = ReservationEvent(
			reservation_event_key("reservation-a", 2),
			"reservation-a",
			2,
			self.key,
			"CNY",
			2,
			"Release Reservation",
			"6.00",
		)
		second = reservation_record(
			release,
			replace(first.source, action="Release Reservation"),
			actor="Administrator",
			occurred_at="2026-10-27 09:31:00.000000",
			previous_digest=first.event_digest,
		)
		new_reserve = ReservationEvent(
			reservation_event_key("reservation-a", 3),
			"reservation-a",
			3,
			self.key,
			"CNY",
			2,
			"Reserve",
			"10.00",
		)
		third = reservation_record(
			new_reserve,
			replace(first.source, action="Reserve Again"),
			actor="Administrator",
			occurred_at="2026-10-27 09:32:00.000000",
			previous_digest=second.event_digest,
		)
		records = [first, second, third]
		anchor = reservation_anchor(
			records,
			expected_source=first.source,
			source_snapshot_digest=self.source.digest(),
			cost_center="center",
		)
		positions = self.observe(
			[frappe._dict(reservation_anchor_row(anchor))],
			[frappe._dict(reservation_record_row(record)) for record in records],
			allocations={"a": self.key},
		)
		self.assertEqual((positions[0].reserved, positions[0].open_reservation), ("16.00", "10.00"))

	def test_orphan_event_poison_guard(self):
		_, event = self.evidence("a", "1.00")
		with self.assertRaises(IoneApplicationError):
			self.observe([], [event])
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_unsupported_event_source_poison_guard(self):
		root, event = self.evidence("a", "1.00")
		event.source_doctype = "Purchase Order"
		with self.assertRaises(IoneApplicationError):
			self.observe([root], [event])
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_source_change_after_read_poison_guard(self):
		changed = replace(self.source, amount="11.000000000")
		with self.assertRaises(IoneApplicationError):
			self.observe([], [], source_reads=[self.source, changed])
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_permission_revocation_after_read_poison_guard(self):
		calls = 0

		def permission_check(*_args):
			nonlocal calls
			calls += 1
			if calls == 2:
				observation.raise_ione_error("PERMISSION_DENIED")

		with self.assertRaises(IoneApplicationError):
			self.observe([], [], permission_check=permission_check)
		self.assertEqual(calls, 2)
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_guest_cannot_read_reservation_rows_even_with_a_guard(self):
		frappe.set_user("Guest")
		with patch.object(observation, "_source_reservations") as read_rows:
			with self.assertRaises(IoneApplicationError) as error:
				observation.guarded_material_reservation_observation(self.source, [self.key], allocations={})
		self.assertEqual(error.exception.code, "IONE-CORE-0002")
		read_rows.assert_not_called()
		frappe.set_user("Administrator")
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_unbound_guard_cannot_acquire_a_purchase_source_late(self):
		frappe.db.rollback()
		acquire_budget_guards([self.key])
		with patch.object(observation, "locked_material_request_source") as read_source:
			with self.assertRaises(IoneApplicationError) as denied:
				observation.guarded_material_reservation_observation(self.source, [self.key], allocations={})
		self.assertEqual(denied.exception.code, "IONE-CORE-0008")
		read_source.assert_not_called()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])

	def test_second_guard_request_does_not_read_source_after_guard(self):
		with patch.object(material_guards, "locked_material_request_source") as read_source:
			with self.assertRaises(IoneApplicationError) as denied:
				material_guards.acquire_material_budget_guards(self.source, [self.key])
		self.assertEqual(denied.exception.code, "IONE-CORE-0008")
		read_source.assert_not_called()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([self.key])
