"""Guarded authorization joins using connection-private synthetic MariaDB tables."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, replace
from datetime import datetime, timedelta
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
	verify_reservation_authorization_rows,
	verify_reservation_confirmation_row,
)
from medops.common.reservation_renewal import (
	RENEWAL_ROW_FIELDS,
	reservation_renewal_decision,
	reservation_renewal_row,
)
from medops.hrp_budget.services import (
	budget_usage_observation,
	material_usage_sources,
	reservation_renewal_lineage,
)
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
			"company": source.company,
			"hospital": source.hospital,
			"organization_unit": source.organization_unit,
			"fiscal_year": "Year",
			"budget_subject": "Subject",
			"funding_source": "Fund",
			"project": source.project,
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
		currency=source.currency,
		precision=2,
		amount=amount,
		budget_date=source.transaction_date,
		reason="Synthetic",
	)
	proof = "synthetic_confirmation_proof_1234567890"
	start = max(datetime.fromisoformat(source.request_modified), datetime.fromisoformat(source.line_modified))
	start += timedelta(microseconds=1)

	def stamp(value):
		return value.isoformat(sep=" ", timespec="microseconds")

	confirmed = reservation_confirmation(
		item,
		actor="manager@example.com",
		proof_hash=hashlib.sha256(proof.encode()).hexdigest(),
		session_digest="a" * 64,
		recorded_at=stamp(start),
		expires_at=stamp(start + timedelta(minutes=5)),
	)
	decision = reservation_decision(
		item,
		confirmed,
		proof=proof,
		actor="manager@example.com",
		session_digest="a" * 64,
		recorded_at=stamp(start + timedelta(microseconds=1)),
		acknowledge=True,
	)
	ref = ReservationSource(
		"Material Request", source.request, digest, source.line, item.allocation, "Reserve"
	)
	name = f"RESERVATION-{index:03d}"
	event = ReservationEvent(
		reservation_event_key(name, 1), name, 1, key, source.currency, 2, "Reserve", amount
	)
	record = reservation_record(
		event,
		ref,
		actor="manager@example.com",
		occurred_at=stamp(start + timedelta(microseconds=2)),
		previous_digest=None,
	)
	anchor = reservation_anchor(
		[record], expected_source=ref, source_snapshot_digest=digest, cost_center=source.cost_center
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
			("tabHRP Budget Reservation Renewal", RENEWAL_ROW_FIELDS),
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

	def renewal_fixture(self):
		source, item, _, _, first, original_row = synthetic_rows()
		_, initial = verify_reservation_confirmation_row(first)
		_, original = verify_reservation_authorization_rows(first, original_row)
		proof = "synthetic_renewal_confirmation_1234567890"
		start = datetime.fromisoformat(original.expires_at)
		confirmed = reservation_confirmation(
			item,
			actor="manager@example.com",
			proof_hash=hashlib.sha256(proof.encode()).hexdigest(),
			session_digest="b" * 64,
			recorded_at=start.isoformat(sep=" ", timespec="microseconds"),
			expires_at=(start + timedelta(minutes=5)).isoformat(sep=" ", timespec="microseconds"),
		)
		decision = reservation_renewal_decision(
			item,
			initial,
			original,
			(),
			confirmed,
			proof=proof,
			actor="manager@example.com",
			session_digest="b" * 64,
			recorded_at=(start + timedelta(seconds=1)).isoformat(sep=" ", timespec="microseconds"),
			acknowledge=True,
		)
		for evidence in (first, original_row, reservation_authorization_row(item, confirmed)):
			self.insert_row("tabHRP Budget Reservation Authorization", evidence)
		self.insert_row(
			"tabHRP Budget Reservation Renewal",
			reservation_renewal_row(item, initial, original, (), confirmed, decision),
		)
		binding = (source.request, source.line, source.organization_unit, source.digest())
		acquire_multi_source_bound_budget_guards([item.key], source_bindings=(binding,))
		return source, item, first, original_row, confirmed, decision

	def test_guarded_renewal_reads_complete_chain_and_rejects_tamper(self):
		source, item, first, original, confirmed, decision = self.renewal_fixture()
		with (
			patch.object(reservation_renewal_lineage, "session_digest", return_value="a" * 64),
			patch.object(reservation_renewal_lineage, "require_reservation_actor_scope"),
			patch.object(reservation_renewal_lineage, "locked_material_request_source", return_value=source),
		):
			observed = reservation_renewal_lineage.locked_renewal_lineage(
				item, source, first["name"], original["name"]
			)
			self.assertEqual(observed.renewals, ((confirmed, decision),))
			assert_budget_guards([item.key])
			# The table is connection-private and contains only synthetic evidence.
			frappe.db.sql(
				"UPDATE `tabHRP Budget Reservation Renewal` SET `previous_decision`=%s", ("f" * 64,)
			)
			with self.assertRaises(IoneApplicationError):
				reservation_renewal_lineage.locked_renewal_lineage(
					item, source, first["name"], original["name"]
				)
			with self.assertRaises(IoneApplicationError):
				assert_budget_guards([item.key])

	def test_guarded_renewal_rejects_missing_confirmation(self):
		source, item, first, original, confirmed, _ = self.renewal_fixture()
		# Only a connection-private synthetic fixture is modified.
		frappe.db.sql(
			"DELETE FROM `tabHRP Budget Reservation Authorization` WHERE `name`=%s", (confirmed.name,)
		)
		with (
			patch.object(reservation_renewal_lineage, "session_digest", return_value="a" * 64),
			patch.object(reservation_renewal_lineage, "require_reservation_actor_scope"),
			patch.object(reservation_renewal_lineage, "locked_material_request_source", return_value=source),
			self.assertRaises(IoneApplicationError),
		):
			reservation_renewal_lineage.locked_renewal_lineage(item, source, first["name"], original["name"])
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([item.key])

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
			by_source = observation.verify_guarded_initial_authorizations(
				[item.key], sources, tuple(positions)
			)
			self.assertEqual(len(by_source[(first.request, first.line, first.organization_unit)]), 51)
			self.assertEqual(len(by_source[(second.request, second.line, second.organization_unit)]), 50)
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

	def verify_progressed_source(self, *, financial_change=False, backdated_field=None):
		historical, item, root, events, confirmation, decision = synthetic_rows()
		current = replace(
			historical,
			status="Ordered",
			request_modified="2026-01-02 09:00:00",
			amount="31.000000000" if financial_change else historical.amount,
		)
		if backdated_field:
			current = replace(current, **{backdated_field: "2025-12-31 23:59:59"})
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
			if financial_change or backdated_field:
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

	def test_historical_decision_rejects_backdated_parent(self):
		self.verify_progressed_source(backdated_field="request_modified")

	def test_historical_decision_rejects_backdated_line(self):
		self.verify_progressed_source(backdated_field="line_modified")
