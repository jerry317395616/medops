"""Guarded authorization joins using connection-private synthetic MariaDB tables."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, replace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.authorized_reservation_projection import verify_authorized_initial_reservation
from medops.common.budget_control import budget_control_key
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS, reservation_anchor
from medops.common.budget_reservation_events import ReservationEvent
from medops.common.budget_reservation_proof import (
	ReservationSource,
	reservation_event_key,
	reservation_record,
)
from medops.common.budget_reservation_storage import ROW_FIELDS, reservation_record_row
from medops.common.error_catalog import IoneApplicationError
from medops.common.material_source_snapshot import (
	build_snapshot_reservation_projection,
	material_source_snapshot,
)
from medops.common.reservation_authorization import (
	AUTHORIZATION_ROW_FIELDS,
	reservation_authorization_row,
	reservation_confirmation,
	reservation_decision,
	reservation_intent,
)
from medops.hrp_budget.services import budget_usage_observation, material_usage_sources
from medops.hrp_budget.services import material_authorization_observation as observation
from medops.hrp_budget.services.control_guards import (
	acquire_multi_source_bound_budget_guards,
	assert_budget_guards,
)
from medops.hrp_budget.services.material_request_source import MaterialRequestSource


def synthetic_rows(*, index=1, source=None, amount="10.00"):
	base_source = MaterialRequestSource(
		request="MR-001",
		line="MR-LINE-001",
		request_modified="2026-01-01 09:00:00",
		line_modified="2026-01-01 09:00:00",
		company="Company",
		hospital="Hospital",
		organization_unit="Unit",
		organization_version="Version",
		mapping_revision=1,
		cost_center="Center",
		currency="CNY",
		transaction_date="2026-01-01",
		docstatus=1,
		status="Pending",
		item_code="Item",
		uom="Unit",
		expense_account="Account",
		project=None,
		quantity="5.000000000",
		rate="6.000000000",
		amount="30.000000000",
	)
	source = source or base_source
	key = budget_control_key(
		{
			"company": "Company",
			"hospital": "Hospital",
			"organization_unit": "Unit",
			"fiscal_year": "Year",
			"budget_subject": "Subject",
			"funding_source": "Fund",
			"project": None,
			"period_start": "2026-01-01",
			"period_end": "2026-12-31",
		}
	)
	snapshot, digest = material_source_snapshot(asdict(source))
	item = reservation_intent(
		source_snapshot=snapshot,
		source_snapshot_digest=digest,
		allocation=f"allocation-{index}",
		key=key,
		currency="CNY",
		precision=2,
		amount=amount,
		budget_date="2026-01-01",
		reason="Synthetic",
	)
	proof = "synthetic_confirmation_proof_1234567890"
	confirmed = reservation_confirmation(
		item,
		actor="manager@example.com",
		proof_hash=hashlib.sha256(proof.encode()).hexdigest(),
		session_digest="a" * 64,
		recorded_at="2026-01-01 10:00:00.000000",
		expires_at="2026-01-01 10:05:00.000000",
	)
	decision = reservation_decision(
		item,
		confirmed,
		proof=proof,
		actor="manager@example.com",
		session_digest="a" * 64,
		recorded_at="2026-01-01 10:01:00.000000",
		acknowledge=True,
	)
	ref = ReservationSource(
		"Material Request", source.request, digest, source.line, item.allocation, "Reserve"
	)
	name = f"RESERVATION-{index:03d}"
	event = ReservationEvent(reservation_event_key(name, 1), name, 1, key, "CNY", 2, "Reserve", amount)
	record = reservation_record(
		event,
		ref,
		actor="manager@example.com",
		occurred_at="2026-01-01 10:02:00.000000",
		previous_digest=None,
	)
	anchor = reservation_anchor(
		[record], expected_source=ref, source_snapshot_digest=digest, cost_center="Center"
	)
	root = build_snapshot_reservation_projection(
		anchor, [record], source_facts=asdict(source), allocation=item.allocation, expected_key=key
	)
	return (
		source,
		item,
		root,
		[reservation_record_row(record)],
		reservation_authorization_row(item, confirmed),
		reservation_authorization_row(item, confirmed, decision),
	)


class TestBudgetMaterialAuthorizationObservation(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		self.tables = (
			("tabHRP Budget Reservation", ANCHOR_ROW_FIELDS | {"source_snapshot_json"}),
			("tabHRP Budget Reservation Event", ROW_FIELDS),
			("tabHRP Budget Reservation Authorization", AUTHORIZATION_ROW_FIELDS),
		)
		integers = {"event_count", "sequence", "currency_precision", "proof_version", "acknowledged"}
		for table, fields in self.tables:
			columns = []
			for name in sorted(fields):
				kind = "INT" if name in integers else "LONGTEXT" if name.endswith("_json") else "VARCHAR(255)"
				columns.append(f"`{name}` {kind}")
			frappe.db.sql(
				f"CREATE TEMPORARY TABLE `{table}` ({', '.join(columns)}) ENGINE=InnoDB"  # nosec B608 - fixed fields
			)

	def tearDown(self):
		frappe.db.rollback()
		for table, _ in reversed(self.tables):
			frappe.db.sql(f"DROP TEMPORARY TABLE `{table}`")  # nosec B608 - fixed temporary tables
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.begin()
		super().tearDown()

	def insert_row(self, table, row):
		fields = sorted(row)
		columns = ", ".join(f"`{field}`" for field in fields)
		placeholders = ", ".join(["%s"] * len(fields))
		frappe.db.sql(
			f"INSERT INTO `{table}` ({columns}) VALUES ({placeholders})",  # nosec B608 - fixed synthetic rows
			tuple(row[field] for field in fields),
		)

	def test_real_sql_requires_one_bound_initial_decision(self):
		source, item, root, events, confirmation, decision = synthetic_rows()
		position, _, _ = verify_authorized_initial_reservation(
			root, events, confirmation, decision, current_facts=asdict(source)
		)
		self.assertEqual(source.digest(), item.source_snapshot_digest)
		self.insert_row("tabHRP Budget Reservation", root)
		self.insert_row("tabHRP Budget Reservation Event", events[0])
		self.insert_row("tabHRP Budget Reservation Authorization", confirmation)
		self.insert_row("tabHRP Budget Reservation Authorization", decision)
		binding = (source.request, source.line, source.organization_unit, source.digest())
		acquire_multi_source_bound_budget_guards([item.key], source_bindings=(binding,))
		with patch.object(observation, "locked_material_request_source", return_value=source):
			observation.verify_guarded_initial_authorizations([item.key], (source,), (position,))
			assert_budget_guards([item.key])
			frappe.db.sql(
				"DELETE FROM `tabHRP Budget Reservation Authorization` WHERE `name`=%s", (decision["name"],)
			)
			with self.assertRaises(IoneApplicationError):
				observation.verify_guarded_initial_authorizations([item.key], (source,), (position,))
			with self.assertRaises(IoneApplicationError):
				assert_budget_guards([item.key])

	def test_batch_crosses_chunk_boundary_without_dropping_authorizations(self):
		first, _, _, _, _, _ = synthetic_rows()
		second = replace(first, request="MR-002", line="MR-LINE-002")
		sources = (first, second)
		positions = []
		item = None
		first_identity = None
		for index in range(1, 102):
			source = first if index <= 51 else second
			_, item, root, events, confirmation, decision = synthetic_rows(
				index=index, source=source, amount="0.10"
			)
			position, _, _ = verify_authorized_initial_reservation(
				root, events, confirmation, decision, current_facts=asdict(source)
			)
			positions.append(position)
			if first_identity is None:
				first_identity = root["source_identity_key"]
			self.insert_row("tabHRP Budget Reservation", root)
			self.insert_row("tabHRP Budget Reservation Event", events[0])
			self.insert_row("tabHRP Budget Reservation Authorization", confirmation)
			self.insert_row("tabHRP Budget Reservation Authorization", decision)
		assert item is not None
		bindings = tuple(
			sorted(
				(source.request, source.line, source.organization_unit, source.digest()) for source in sources
			)
		)
		acquire_multi_source_bound_budget_guards([item.key], source_bindings=bindings)
		by_id = {(source.request, source.line, source.organization_unit): source for source in sources}
		with patch.object(
			observation, "locked_material_request_source", side_effect=lambda *identity: by_id[identity]
		) as read:
			observation.verify_guarded_initial_authorizations([item.key], sources, tuple(positions))
			self.assertEqual(read.call_count, 4)
			assert_budget_guards([item.key])
			frappe.db.sql(
				"UPDATE `tabHRP Budget Reservation` SET `source_identity_key`=%s WHERE `name`=%s",
				(first_identity, root["name"]),
			)
			with self.assertRaises(IoneApplicationError):
				observation.verify_guarded_initial_authorizations([item.key], sources, tuple(positions))
			with self.assertRaises(IoneApplicationError):
				assert_budget_guards([item.key])

	def verify_progressed_source(self, *, financial_change=False):
		historical, item, root, events, confirmation, decision = synthetic_rows()
		current = replace(
			historical,
			status="Ordered",
			request_modified="2026-01-02 09:00:00",
			amount="31.000000000" if financial_change else historical.amount,
		)
		self.insert_row("tabHRP Budget Reservation", root)
		self.insert_row("tabHRP Budget Reservation Event", events[0])
		self.insert_row("tabHRP Budget Reservation Authorization", confirmation)
		self.insert_row("tabHRP Budget Reservation Authorization", decision)
		with patch.object(budget_usage_observation, "check_usage_access"):
			discovered = material_usage_sources.discover_material_usage_sources([item.key])
		self.assertEqual(discovered[0].expected_digest, historical.digest())
		binding = (current.request, current.line, current.organization_unit, current.digest())
		acquire_multi_source_bound_budget_guards([item.key], source_bindings=(binding,))
		with (
			patch.object(budget_usage_observation, "check_usage_access"),
			patch.object(material_usage_sources, "locked_material_request_source", return_value=current),
			patch.object(observation, "locked_material_request_source", return_value=current),
		):
			positions = material_usage_sources.guarded_verify_provisional_material_usage_sources(
				[item.key], (current,), discovered=discovered
			)
			if financial_change:
				with self.assertRaises(IoneApplicationError):
					observation.verify_guarded_initial_authorizations([item.key], (current,), positions)
				with self.assertRaises(IoneApplicationError):
					assert_budget_guards([item.key])
			else:
				observation.verify_guarded_initial_authorizations([item.key], (current,), positions)
				assert_budget_guards([item.key])
				with self.assertRaises(IoneApplicationError):
					material_usage_sources.guarded_verify_material_usage_sources(
						[item.key], (current,), discovered=discovered
					)

	def test_historical_decision_allows_only_nonfinancial_source_progress(self):
		self.verify_progressed_source()

	def test_historical_decision_rejects_changed_financial_source(self):
		self.verify_progressed_source(financial_change=True)
