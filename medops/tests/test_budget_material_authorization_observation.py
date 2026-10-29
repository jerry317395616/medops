"""Guarded authorization joins using connection-private synthetic MariaDB tables."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.authorized_reservation_projection import (
	verify_authorized_initial_reservation,
	verify_authorized_released_reservation,
	verify_renewed_initial_reservation,
)
from medops.common.budget_control import budget_control_key, ordered_budget_keys
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS, reservation_anchor
from medops.common.budget_reservation_events import ReservationEvent
from medops.common.budget_reservation_proof import (
	ReservationSource,
	reservation_event_key,
	reservation_record,
	verify_reservation_records,
)
from medops.common.budget_reservation_storage import (
	ROW_FIELDS,
	reservation_record_from_row,
	reservation_record_row,
)
from medops.common.commitment_evidence import COMMITMENT_ROW_FIELDS, commitment_sources
from medops.common.error_catalog import IoneApplicationError
from medops.common.initial_reservation_posting import (
	initial_reservation_documents,
	renewed_initial_reservation_documents,
)
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
from medops.common.reservation_commitment import reservation_commitment_intent
from medops.common.reservation_release import RELEASE_ROW_FIELDS
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
from medops.hrp_budget.services import reservation_commitment_allocations as allocations
from medops.hrp_budget.services import reservation_commitment_state as commitment_state
from medops.hrp_budget.services.control_guards import (
	acquire_multi_source_bound_budget_guards,
	assert_budget_guards,
)
from medops.hrp_budget.services.material_request_source import MaterialRequestSource
from medops.hrp_budget.services.material_usage_preview import PurchaseUsageInspection
from medops.hrp_budget.services.purchase_order_source import PurchaseOrderSource
from medops.hrp_budget.services.reservation_action_observation import guarded_commitment_decision_inventory
from medops.hrp_budget.services.reservation_receipt_history import read_locked_initial_receipt_history
from medops.services.errors import raise_ione_error
from medops.tests.commitment_evidence_fixtures import history_commitment
from medops.tests.reservation_release_fixtures import release_evidence
from medops.tests.reservation_release_fixtures import rows as release_rows


def synthetic_rows(*, index=1, source=None, amount="10.00", reservation_name=None, key=None):
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
	key = key or budget_control_key(
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
	name = reservation_name or f"RESERVATION-{index:03d}"
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


def synthetic_renewed_rows(*, index=1, source=None, amount="10.00"):
	"""Build checked synthetic event and authorization rows without real writes."""
	source, item, _, _, first, original_row = synthetic_rows(index=index, source=source, amount=amount)
	_, initial = verify_reservation_confirmation_row(first)
	_, original = verify_reservation_authorization_rows(first, original_row)
	proof = "synthetic_renewal_confirmation_1234567890"
	start = datetime.fromisoformat(original.expires_at)
	confirmed = reservation_confirmation(
		item,
		actor="renewing-manager@example.com",
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
		actor="renewing-manager@example.com",
		session_digest="b" * 64,
		recorded_at=(start + timedelta(seconds=1)).isoformat(sep=" ", timespec="microseconds"),
		acknowledge=True,
	)
	documents = renewed_initial_reservation_documents(
		first,
		original_row,
		[(confirmed, decision)],
		occurred_at=(start + timedelta(seconds=2)).isoformat(sep=" ", timespec="microseconds"),
	)
	root = {field: documents.root[field] for field in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}}
	events = [dict(documents.event)]
	new_confirmation = reservation_authorization_row(item, confirmed)
	renewal_row = reservation_renewal_row(item, initial, original, (), confirmed, decision)
	return source, item, root, events, first, original_row, new_confirmation, renewal_row, confirmed, decision


class TestBudgetMaterialAuthorizationObservation(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		access_patch = patch.object(budget_usage_observation, "check_usage_access")
		access_patch.start()
		self.addCleanup(access_patch.stop)
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		self.tables = (
			("tabHRP Budget Reservation", ANCHOR_ROW_FIELDS | {"source_snapshot_json"}),
			("tabHRP Budget Reservation Event", ROW_FIELDS),
			("tabHRP Budget Reservation Authorization", AUTHORIZATION_ROW_FIELDS),
			("tabHRP Budget Reservation Renewal", RENEWAL_ROW_FIELDS),
			("tabHRP Budget Reservation Release", RELEASE_ROW_FIELDS),
			("tabHRP Budget Commitment Authorization", COMMITMENT_ROW_FIELDS),
		)
		integers = {
			"event_count",
			"sequence",
			"previous_sequence",
			"currency_precision",
			"proof_version",
			"acknowledged",
		}
		for table, fields in self.tables:
			if table in {"tabHRP Budget Reservation Release", "tabHRP Budget Commitment Authorization"}:
				# Preserve the actual engine, column types and unique/index contracts.
				# A hand-built shadow cannot represent the storage required by readers.
				frappe.db.sql(f"CREATE TEMPORARY TABLE `medops_action_structure_template` LIKE `{table}`")  # nosec B608 - fixed private table list
				try:
					frappe.db.sql(
						f"CREATE TEMPORARY TABLE `{table}` LIKE `medops_action_structure_template`"  # nosec B608 - fixed private table list
					)
				finally:
					frappe.db.sql("DROP TEMPORARY TABLE `medops_action_structure_template`")
				continue
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

	def released_fixture(self, *, renewed=False, full=False):
		"""Append synthetic rows only to this connection's private evidence tables."""
		if renewed:
			fixture = synthetic_renewed_rows()
			source, item, root, events, first, original, _, _, confirmed, decision = fixture
			self.insert_renewed_fixture(fixture)
			lineage = [(confirmed, decision)]
		else:
			source, item, root, events, first, original = synthetic_rows()
			for table, row in (
				("tabHRP Budget Reservation", root),
				("tabHRP Budget Reservation Event", events[0]),
				("tabHRP Budget Reservation Authorization", first),
				("tabHRP Budget Reservation Authorization", original),
			):
				self.insert_row(table, row)
			lineage = []
		records = [reservation_record_from_row(events[0])]
		pairs = []
		for amount, minute in (("4.00", 10), ("6.00", 20)) if full else (("4.00", 10),):
			intent, confirmed, decision, record = release_evidence(item, records, amount, minute)
			pair = release_rows(intent, confirmed, decision)
			for row in pair:
				self.insert_row("tabHRP Budget Reservation Release", row)
			self.insert_row("tabHRP Budget Reservation Event", reservation_record_row(record))
			pairs.append(pair)
			records.append(record)
		position, _, _ = verify_authorized_released_reservation(
			root,
			[reservation_record_row(record) for record in records],
			first,
			original,
			pairs,
			current_facts=asdict(source),
			renewals=lineage,
		)
		return source, item, root, records, pairs, position

	def bind_release_source(self, source, item):
		binding = (source.request, source.line, source.organization_unit, source.digest())
		acquire_multi_source_bound_budget_guards([item.key], source_bindings=(binding,))

	def test_receipt_history_accepts_release_tails_only_after_complete_inspection(self):
		source, item, root, records, _, expected = self.released_fixture(full=True)
		self.bind_release_source(source, item)
		first_event = records[0].event.name
		with self.assertRaises(ValueError):
			read_locked_initial_receipt_history(item, source, root["name"], first_event)
		rows, pairs, commitments = read_locked_initial_receipt_history(
			item, source, root["name"], first_event, observed_position=expected
		)
		self.assertEqual((len(rows), len(pairs)), (3, 2))
		self.assertEqual(commitments, [])
		self.assertEqual(rows[0]["name"], first_event)
		assert_budget_guards([item.key])

	def test_receipt_history_rejects_stale_position_and_substituted_first_event(self):
		source, item, root, records, _, expected = self.released_fixture(full=True)
		self.bind_release_source(source, item)
		for position, event in (
			(verify_reservation_records(records[:1]), records[0].event.name),
			(replace(expected, open_reservation="9.00"), records[0].event.name),
			(expected, records[-1].event.name),
		):
			with self.subTest(position=position.last_sequence, event=event), self.assertRaises(ValueError):
				read_locked_initial_receipt_history(
					item, source, root["name"], event, observed_position=position
				)

	def test_full_release_reads_current_position_without_mutating_frozen_root(self):
		source, item, root, _, _, expected = self.released_fixture(full=True)
		discovered = material_usage_sources.discover_material_usage_sources([item.key])
		self.bind_release_source(source, item)
		with (
			patch.object(material_usage_sources, "locked_material_request_source", return_value=source),
			patch.object(observation, "locked_material_request_source", return_value=source),
		):
			positions = material_usage_sources.guarded_verify_provisional_material_usage_sources(
				[item.key],
				(source,),
				discovered=discovered,
			)
			self.assertEqual(positions, (expected,))
			by_source = observation.verify_guarded_initial_authorizations([item.key], (source,), positions)
			self.assertEqual(by_source[(source.request, source.line, source.organization_unit)], (expected,))
		self.assertEqual((expected.last_sequence, expected.open_reservation), (3, "0.00"))
		stored = frappe.db.sql("SELECT `event_count`, `reserved_amount` FROM `tabHRP Budget Reservation`")
		self.assertEqual(stored, ((1, root["reserved_amount"]),))
		assert_budget_guards([item.key])

	def test_released_renewed_root_accepts_nonfinancial_source_progress(self):
		historical, item, _, _, _, expected = self.released_fixture(renewed=True)
		current = replace(historical, status="Ordered", request_modified="2026-01-02 09:00:00")
		discovered = material_usage_sources.discover_material_usage_sources([item.key])
		self.bind_release_source(current, item)
		with (
			patch.object(material_usage_sources, "locked_material_request_source", return_value=current),
			patch.object(observation, "locked_material_request_source", return_value=current),
		):
			positions = material_usage_sources.guarded_verify_provisional_material_usage_sources(
				[item.key],
				(current,),
				discovered=discovered,
			)
			result = observation.verify_guarded_initial_authorizations([item.key], (current,), positions)
			self.assertEqual(result[(current.request, current.line, current.organization_unit)], (expected,))
			self.assertEqual(expected.open_reservation, "6.00")
		assert_budget_guards([item.key])

	def test_release_tail_without_decision_invalidates_budget_guard(self):
		source, item, _, _, pairs, _ = self.released_fixture()
		frappe.db.sql(
			"DELETE FROM `tabHRP Budget Reservation Release` WHERE `name`=%s", (pairs[0][1]["name"],)
		)
		self.bind_release_source(source, item)
		with self.assertRaises(IoneApplicationError) as rejected:
			budget_usage_observation.guarded_budget_usage_observation([item.key])
		self.assertEqual(rejected.exception.definition.key, "CONFLICT")
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([item.key])

	def test_posted_release_decision_is_not_ignored_when_its_event_is_missing(self):
		source, item, _, records, _, _ = self.released_fixture()
		frappe.db.sql("DELETE FROM `tabHRP Budget Reservation Event` WHERE `sequence`>1")
		self.bind_release_source(source, item)
		initial = verify_reservation_records(records[:1])
		with (
			patch.object(observation, "locked_material_request_source", return_value=source),
			self.assertRaises(IoneApplicationError) as rejected,
		):
			observation.verify_guarded_initial_authorizations([item.key], (source,), (initial,))
		self.assertEqual(rejected.exception.definition.key, "CONFLICT")
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([item.key])

	def test_missing_independent_release_confirmation_invalidates_guard(self):
		source, item, _, _, pairs, expected = self.released_fixture()
		frappe.db.sql(
			"DELETE FROM `tabHRP Budget Reservation Release` WHERE `name`=%s", (pairs[0][0]["name"],)
		)
		self.bind_release_source(source, item)
		with (
			patch.object(observation, "locked_material_request_source", return_value=source),
			self.assertRaises(IoneApplicationError) as rejected,
		):
			observation.verify_guarded_initial_authorizations([item.key], (source,), (expected,))
		self.assertEqual(rejected.exception.definition.key, "CONFLICT")
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([item.key])

	def test_release_scope_mismatch_is_found_by_parent_reverse_query(self):
		source, item, _, _, pairs, _ = self.released_fixture()
		frappe.db.sql(
			"UPDATE `tabHRP Budget Reservation Release` SET `budget_subject`=%s WHERE `name`=%s",
			("different-subject", pairs[0][1]["name"]),
		)
		self.bind_release_source(source, item)
		with self.assertRaises(IoneApplicationError) as rejected:
			budget_usage_observation.guarded_budget_usage_observation([item.key])
		self.assertEqual(rejected.exception.definition.key, "CONFLICT")
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([item.key])

	def test_orphan_release_decision_is_found_without_any_roots_or_events(self):
		source, item, _, _, _, _ = self.released_fixture()
		frappe.db.sql("DELETE FROM `tabHRP Budget Reservation Event`")
		frappe.db.sql("DELETE FROM `tabHRP Budget Reservation`")
		self.bind_release_source(source, item)
		with self.assertRaises(IoneApplicationError) as rejected:
			budget_usage_observation.guarded_budget_usage_observation([item.key])
		self.assertEqual(rejected.exception.definition.key, "CONFLICT")
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([item.key])

	def test_release_on_101st_root_is_verified_across_batches_and_overflow_rejects(self):
		from medops.hrp_budget.services import reservation_release_observation

		first_source, _, _, _, _, _ = synthetic_rows()
		second_source = replace(first_source, request="MR-002", line="MR-LINE-002")
		sources = (first_source, second_source)
		for index in range(1, 102):
			source = first_source if index <= 51 else second_source
			_, item, root, events, first, original = synthetic_rows(index=index, source=source, amount="0.10")
			for table, row in (
				("tabHRP Budget Reservation", root),
				("tabHRP Budget Reservation Event", events[0]),
				("tabHRP Budget Reservation Authorization", first),
				("tabHRP Budget Reservation Authorization", original),
			):
				self.insert_row(table, row)
			if index == 101:
				intent, confirmed, decided, record = release_evidence(
					item, [reservation_record_from_row(events[0])], "0.04"
				)
				for row in release_rows(intent, confirmed, decided):
					self.insert_row("tabHRP Budget Reservation Release", row)
				self.insert_row("tabHRP Budget Reservation Event", reservation_record_row(record))
		discovered = material_usage_sources.discover_material_usage_sources([item.key])
		bindings = tuple(
			sorted(
				(source.request, source.line, source.organization_unit, source.digest()) for source in sources
			)
		)
		acquire_multi_source_bound_budget_guards([item.key], source_bindings=bindings)
		by_id = {(source.request, source.line, source.organization_unit): source for source in sources}
		with (
			patch.object(
				material_usage_sources,
				"locked_material_request_source",
				side_effect=lambda *identity: by_id[identity],
			),
			patch.object(
				observation, "locked_material_request_source", side_effect=lambda *identity: by_id[identity]
			),
		):
			positions = material_usage_sources.guarded_verify_provisional_material_usage_sources(
				[item.key], sources, discovered=discovered
			)
			result = observation.verify_guarded_initial_authorizations([item.key], sources, positions)
			self.assertEqual(sum(len(values) for values in result.values()), 101)
			last = next(position for position in positions if position.reservation == "RESERVATION-101")
			self.assertEqual((last.last_sequence, last.open_reservation), (2, "0.06"))
		assert_budget_guards([item.key])
		with (
			patch.object(reservation_release_observation, "MAX_RELEASE_DECISIONS", 0),
			self.assertRaises(IoneApplicationError),
		):
			budget_usage_observation.guarded_budget_usage_observation([item.key])
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([item.key])

	def renewal_fixture(self, *, progressed=False):
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
		if progressed:
			source = replace(source, status="Ordered", request_modified="2026-01-02 09:00:00")
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

	def test_guarded_renewal_replay_allows_only_nonfinancial_source_progress(self):
		source, item, first, original, confirmed, decision = self.renewal_fixture(progressed=True)
		with (
			patch.object(reservation_renewal_lineage, "session_digest", return_value="a" * 64),
			patch.object(reservation_renewal_lineage, "require_reservation_actor_scope"),
			patch.object(reservation_renewal_lineage, "locked_material_request_source", return_value=source),
		):
			observed = reservation_renewal_lineage.locked_renewal_lineage(
				item, source, first["name"], original["name"], allow_progress=True
			)
			self.assertEqual(observed.renewals, ((confirmed, decision),))
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

	def insert_renewed_fixture(self, fixture):
		_, _, root, events, first, original, new_confirmation, renewal, _, _ = fixture
		for table, row in (
			("tabHRP Budget Reservation", root),
			("tabHRP Budget Reservation Event", events[0]),
			("tabHRP Budget Reservation Authorization", first),
			("tabHRP Budget Reservation Authorization", original),
			("tabHRP Budget Reservation Authorization", new_confirmation),
			("tabHRP Budget Reservation Renewal", renewal),
		):
			self.insert_row(table, row)

	def test_renewed_event_accepts_progressed_source_then_rejects_tampered_chain(self):
		fixture = synthetic_renewed_rows()
		historical, item, root, events, first, original, _, _, confirmed, decision = fixture
		current = replace(historical, status="Ordered", request_modified="2026-01-02 09:00:00")
		position, _, _ = verify_renewed_initial_reservation(
			root, events, first, original, [(confirmed, decision)], current_facts=asdict(current)
		)
		self.insert_renewed_fixture(fixture)
		binding = (current.request, current.line, current.organization_unit, current.digest())
		acquire_multi_source_bound_budget_guards([item.key], source_bindings=(binding,))
		with patch.object(observation, "locked_material_request_source", return_value=current):
			by_source = observation.verify_guarded_initial_authorizations([item.key], (current,), (position,))
			self.assertEqual(
				by_source[(current.request, current.line, current.organization_unit)], (position,)
			)
			# The target is a connection-private table containing synthetic evidence.
			frappe.db.sql("UPDATE `tabHRP Budget Reservation Renewal` SET `event_digest`=%s", ("f" * 64,))
			with self.assertRaises(IoneApplicationError):
				observation.verify_guarded_initial_authorizations([item.key], (current,), (position,))
			with self.assertRaises(IoneApplicationError):
				assert_budget_guards([item.key])

	def test_revoked_read_access_invalidates_renewed_history_guard(self):
		fixture = synthetic_renewed_rows()
		source, item, root, events, first, original, _, _, confirmed, decision = fixture
		position, _, _ = verify_renewed_initial_reservation(
			root, events, first, original, [(confirmed, decision)], current_facts=asdict(source)
		)
		self.insert_renewed_fixture(fixture)
		binding = (source.request, source.line, source.organization_unit, source.digest())
		acquire_multi_source_bound_budget_guards([item.key], source_bindings=(binding,))
		calls = []

		def revoke_after_read(*args):
			calls.append(args)
			if len(calls) > 1:
				raise_ione_error("PERMISSION_DENIED")

		with (
			patch.object(budget_usage_observation, "check_usage_access", side_effect=revoke_after_read),
			patch.object(observation, "locked_material_request_source", return_value=source),
		):
			with self.assertRaises(IoneApplicationError):
				observation.verify_guarded_initial_authorizations([item.key], (source,), (position,))
			self.assertEqual(len(calls), 2)
			with self.assertRaises(IoneApplicationError):
				assert_budget_guards([item.key])

	def test_renewed_event_rejects_missing_independent_confirmation(self):
		fixture = synthetic_renewed_rows()
		source, item, root, events, first, original, new_confirmation, _, confirmed, decision = fixture
		position, _, _ = verify_renewed_initial_reservation(
			root, events, first, original, [(confirmed, decision)], current_facts=asdict(source)
		)
		self.insert_renewed_fixture(fixture)
		frappe.db.sql(
			"DELETE FROM `tabHRP Budget Reservation Authorization` WHERE `name`=%s",
			(new_confirmation["name"],),
		)
		binding = (source.request, source.line, source.organization_unit, source.digest())
		acquire_multi_source_bound_budget_guards([item.key], source_bindings=(binding,))
		with patch.object(observation, "locked_material_request_source", return_value=source):
			with self.assertRaises(IoneApplicationError):
				observation.verify_guarded_initial_authorizations([item.key], (source,), (position,))
			with self.assertRaises(IoneApplicationError):
				assert_budget_guards([item.key])

	def test_original_event_rejects_unexpected_renewal_successor(self):
		fixture = synthetic_renewed_rows()
		source, item, _, _, _, _, new_confirmation, renewal, _, _ = fixture
		_, _, root, events, first, original = synthetic_rows()
		position, _, _ = verify_authorized_initial_reservation(
			root, events, first, original, current_facts=asdict(source)
		)
		for table, row in (
			("tabHRP Budget Reservation", root),
			("tabHRP Budget Reservation Event", events[0]),
			("tabHRP Budget Reservation Authorization", first),
			("tabHRP Budget Reservation Authorization", original),
			("tabHRP Budget Reservation Authorization", new_confirmation),
			("tabHRP Budget Reservation Renewal", renewal),
		):
			self.insert_row(table, row)
		binding = (source.request, source.line, source.organization_unit, source.digest())
		acquire_multi_source_bound_budget_guards([item.key], source_bindings=(binding,))
		with patch.object(observation, "locked_material_request_source", return_value=source):
			with self.assertRaises(IoneApplicationError):
				observation.verify_guarded_initial_authorizations([item.key], (source,), (position,))
			with self.assertRaises(IoneApplicationError):
				assert_budget_guards([item.key])

	def test_renewed_event_after_100_original_events_is_not_skipped(self):
		source, item, _, _, _, _ = synthetic_rows()
		positions = []
		for index in range(1, 101):
			_, item, root, events, first, original = synthetic_rows(
				index=index, source=source, amount="0.10", reservation_name=f"000-RESERVATION-{index:03d}"
			)
			position, _, _ = verify_authorized_initial_reservation(
				root, events, first, original, current_facts=asdict(source)
			)
			positions.append(position)
			for table, row in (
				("tabHRP Budget Reservation", root),
				("tabHRP Budget Reservation Event", events[0]),
				("tabHRP Budget Reservation Authorization", first),
				("tabHRP Budget Reservation Authorization", original),
			):
				self.insert_row(table, row)
		fixture = synthetic_renewed_rows(index=101, source=source, amount="0.10")
		_, item, root, events, first, original, _, _, confirmed, decision = fixture
		self.assertGreater(root["name"], "000-RESERVATION-100")
		position, _, _ = verify_renewed_initial_reservation(
			root, events, first, original, [(confirmed, decision)], current_facts=asdict(source)
		)
		positions.append(position)
		self.insert_renewed_fixture(fixture)
		binding = (source.request, source.line, source.organization_unit, source.digest())
		acquire_multi_source_bound_budget_guards([item.key], source_bindings=(binding,))
		with patch.object(observation, "locked_material_request_source", return_value=source):
			result = observation.verify_guarded_initial_authorizations(
				[item.key], (source,), tuple(positions)
			)
			self.assertEqual(len(result[(source.request, source.line, source.organization_unit)]), 101)
			self.assertIn(position, result[(source.request, source.line, source.organization_unit)])

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

	def committed_fixture(self, *, mixed=False, renewed=False):
		if renewed:
			fixture = synthetic_renewed_rows()
			source, item, root, events, first, original, _, _, confirmed, decision = fixture
			self.insert_renewed_fixture(fixture)
			lineage = [(confirmed, decision)]
		else:
			source, item, root, events, first, original = synthetic_rows()
			documents = initial_reservation_documents(first, original, occurred_at=events[0]["occurred_at"])
			root = {field: documents.root[field] for field in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}}
			events = [dict(documents.event)]
			for table, row in (
				("tabHRP Budget Reservation", root),
				("tabHRP Budget Reservation Event", events[0]),
				("tabHRP Budget Reservation Authorization", first),
				("tabHRP Budget Reservation Authorization", original),
			):
				self.insert_row(table, row)
			lineage = []
		records = [reservation_record_from_row(events[0])]
		releases, commitments = [], []
		for number in range(2 if mixed else 1):
			_, pair, record = history_commitment(item, records, amount="4.00")
			for row in pair:
				self.insert_row("tabHRP Budget Commitment Authorization", row)
			self.insert_row("tabHRP Budget Reservation Event", reservation_record_row(record))
			records.append(record)
			commitments.append(pair)
			if mixed and number == 0:
				intent, confirmed, decided, released = release_evidence(item, records, "2.00", 20)
				pair = release_rows(intent, confirmed, decided)
				for row in pair:
					self.insert_row("tabHRP Budget Reservation Release", row)
				self.insert_row("tabHRP Budget Reservation Event", reservation_record_row(released))
				records.append(released)
				releases.append(pair)
		position, _, _ = verify_authorized_released_reservation(
			root,
			[reservation_record_row(r) for r in records],
			first,
			original,
			releases,
			commitments=commitments,
			current_facts=asdict(source),
			renewals=lineage,
		)
		return source, item, root, records, releases, commitments, position

	def observe_commitment(self, source, item):
		self.bind_release_source(source, item)
		positions = budget_usage_observation.guarded_budget_usage_observation([item.key])
		with patch.object(observation, "locked_material_request_source", return_value=source):
			checked = observation.verify_guarded_initial_authorizations([item.key], (source,), positions)
		return positions, checked

	def test_mixed_commits_releases_and_old_receipt_read_preserve_frozen_root(self):
		source, item, root, records, releases, commitments, expected = self.committed_fixture(mixed=True)
		positions, _ = self.observe_commitment(source, item)
		self.assertEqual(positions, (expected,))
		rows, checked_releases, checked_commits = read_locked_initial_receipt_history(
			item, source, root["name"], records[0].event.name, observed_position=expected
		)
		self.assertEqual((len(rows), len(checked_releases), len(checked_commits)), (4, 1, 2))
		self.assertEqual((expected.open_reservation, expected.open_commitment), ("0.00", "8.00"))
		stored = frappe.db.sql(
			"SELECT event_count,reserved_amount FROM `tabHRP Budget Reservation` WHERE name=%s",
			(root["name"],),
		)
		self.assertEqual((int(stored[0][0]), str(stored[0][1])), (1, "10.00"))
		self.assertEqual((len(releases), len(commitments)), (1, 2))
		assert_budget_guards([item.key])

	def test_missing_commitment_decision_invalidates_usage_guard(self):
		source, item, _, _, _, pairs, _ = self.committed_fixture()
		frappe.db.sql(
			"DELETE FROM `tabHRP Budget Commitment Authorization` WHERE name=%s", (pairs[0][1]["name"],)
		)
		with self.assertRaises(IoneApplicationError):
			self.observe_commitment(source, item)
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([item.key])

	def test_commitment_decision_without_event_is_not_ignored(self):
		source, item, _, records, _, _, _ = self.committed_fixture()
		frappe.db.sql(
			"DELETE FROM `tabHRP Budget Reservation Event` WHERE name=%s", (records[-1].event.name,)
		)
		with self.assertRaises(IoneApplicationError):
			self.observe_commitment(source, item)

	def test_orphan_commitment_decision_is_found_without_any_roots(self):
		source, item, _, _, _, _, _ = self.committed_fixture()
		frappe.db.sql("DELETE FROM `tabHRP Budget Reservation Event`")
		frappe.db.sql("DELETE FROM `tabHRP Budget Reservation`")
		with self.assertRaises(IoneApplicationError):
			self.observe_commitment(source, item)

	def test_missing_independent_commitment_confirmation_invalidates_guard(self):
		source, item, _, _, _, pairs, _ = self.committed_fixture()
		frappe.db.sql(
			"DELETE FROM `tabHRP Budget Commitment Authorization` WHERE name=%s", (pairs[0][0]["name"],)
		)
		with self.assertRaises(IoneApplicationError):
			self.observe_commitment(source, item)
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([item.key])

	def test_commitment_private_order_snapshot_tamper_is_rejected(self):
		source, item, _, _, _, pairs, _ = self.committed_fixture()
		frappe.db.sql(
			"UPDATE `tabHRP Budget Commitment Authorization` SET order_snapshot_json=CONCAT(order_snapshot_json,' ') WHERE name=%s",
			(pairs[0][0]["name"],),
		)
		with self.assertRaises(IoneApplicationError):
			self.observe_commitment(source, item)

	def test_commitment_scope_mismatch_is_found_by_reverse_parent_read(self):
		source, item, _, _, _, pairs, _ = self.committed_fixture()
		frappe.db.sql(
			"UPDATE `tabHRP Budget Commitment Authorization` SET funding_source=%s WHERE name=%s",
			("Other Fund", pairs[0][1]["name"]),
		)
		with self.assertRaises(IoneApplicationError):
			self.observe_commitment(source, item)

	def test_renewed_initial_receipt_accepts_independent_commitment_tail(self):
		source, item, root, records, _, _, expected = self.committed_fixture(renewed=True)
		positions, _ = self.observe_commitment(source, item)
		self.assertEqual(positions, (expected,))
		rows, _, pairs = read_locked_initial_receipt_history(
			item, source, root["name"], records[0].event.name, observed_position=expected
		)
		self.assertEqual((rows[0]["proof_version"], len(pairs)), (2, 1))

	def test_release_and_commitment_decisions_cannot_share_one_physical_successor(self):
		source, item, _, records, _, _, _ = self.committed_fixture()
		intent, confirmed, decided, _ = release_evidence(item, records[:1], "4.00", 20)
		for row in release_rows(intent, confirmed, decided):
			self.insert_row("tabHRP Budget Reservation Release", row)
		with self.assertRaises(IoneApplicationError):
			self.observe_commitment(source, item)

	def test_unknown_actual_event_cannot_use_a_commitment_decision(self):
		source, item, _, records, _, _, _ = self.committed_fixture()
		previous = records[-1]
		event = replace(
			previous.event,
			name=reservation_event_key(previous.event.reservation, 3),
			sequence=3,
			kind="Actual",
			amount="1.00",
		)
		forged = reservation_record(
			event,
			replace(previous.source, action="Unsupported actual"),
			actor=previous.actor,
			occurred_at=previous.occurred_at,
			previous_digest=previous.event_digest,
		)
		self.insert_row("tabHRP Budget Reservation Event", reservation_record_row(forged))
		with self.assertRaises(IoneApplicationError):
			self.observe_commitment(source, item)

	def test_commitment_scope_inventory_does_not_truncate_after_first_hundred(self):
		source, item, _, _, _, pairs, _ = self.committed_fixture()
		keys = [replace(item.key, funding_source=f"AAA-{i:03}") for i in range(100)] + [item.key]
		binding = (source.request, source.line, source.organization_unit, source.digest())
		acquire_multi_source_bound_budget_guards(keys, source_bindings=(binding,))
		found = guarded_commitment_decision_inventory(keys, [])
		self.assertEqual(set(found), {pairs[0][1]["name"]})

	def quota_fixture(
		self,
		*,
		amount="4.00",
		commits=("4.00", "4.00"),
		order_changes=None,
		other_line=False,
		purchase_changes=None,
		prebind=True,
		guard_all=True,
	):
		source, base, _, _, _, _ = synthetic_rows()
		keys = [base.key, replace(base.key, funding_source="Other Fund")]
		changes = {"quantity": "2.000000000", "amount": "12.000000000", "base_amount": "12.000000000"} | (
			order_changes or {}
		)
		pairs = []
		selected = None
		for index, key in enumerate(keys, 1):
			_, original, _, events, first, second = synthetic_rows(index=index, source=source, key=key)
			documents = initial_reservation_documents(first, second, occurred_at=events[0]["occurred_at"])
			root = {field: documents.root[field] for field in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}}
			first_record = reservation_record_from_row(documents.event)
			_, pair, record = history_commitment(
				original, [first_record], amount=commits[index - 1], order_changes=changes
			)
			for table, row in (
				("tabHRP Budget Reservation", root),
				("tabHRP Budget Reservation Event", dict(documents.event)),
				("tabHRP Budget Reservation Authorization", first),
				("tabHRP Budget Reservation Authorization", second),
				("tabHRP Budget Commitment Authorization", pair[0]),
				("tabHRP Budget Commitment Authorization", pair[1]),
				("tabHRP Budget Reservation Event", reservation_record_row(record)),
			):
				self.insert_row(table, row)
			pairs.append(pair)
			if selected is None:
				selected = original, (first_record, record)
		if other_line:
			key = replace(base.key, funding_source="Third Fund")
			keys.append(key)
			_, original, _, events, first, second = synthetic_rows(index=3, source=source, key=key)
			documents = initial_reservation_documents(first, second, occurred_at=events[0]["occurred_at"])
			root = {field: documents.root[field] for field in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}}
			_, pair, record = history_commitment(
				original,
				[reservation_record_from_row(documents.event)],
				amount="4.00",
				order_changes=changes | {"line": "OTHER-PO-LINE"},
			)
			for table, row in (
				("tabHRP Budget Reservation", root),
				("tabHRP Budget Reservation Event", dict(documents.event)),
				("tabHRP Budget Reservation Authorization", first),
				("tabHRP Budget Reservation Authorization", second),
				("tabHRP Budget Commitment Authorization", pair[0]),
				("tabHRP Budget Commitment Authorization", pair[1]),
				("tabHRP Budget Reservation Event", reservation_record_row(record)),
			):
				self.insert_row(table, row)
		assert selected is not None
		original, records = selected
		from medops.common.commitment_evidence import verify_commitment_evidence_row

		committed, _ = verify_commitment_evidence_row(pairs[0][0])
		purchase = PurchaseOrderSource(
			**commitment_sources(
				committed, pairs[0][0]["material_snapshot_json"], pairs[0][0]["order_snapshot_json"]
			)
		)
		if purchase_changes:
			purchase = replace(purchase, **purchase_changes)
		keys = ordered_budget_keys(keys)
		if not guard_all:
			keys = keys[:1]
		discovered = material_usage_sources.discover_material_usage_sources(keys)
		binding = (source.request, source.line, source.organization_unit, source.digest())
		acquire_multi_source_bound_budget_guards(
			keys, source_bindings=(binding,), purchase_parents=(purchase.order,) if prebind else ()
		)
		positions = budget_usage_observation.guarded_budget_usage_observation(keys)
		snapshot, digest = purchase.commitment_snapshot(source)
		proposed = reservation_commitment_intent(
			original,
			records,
			order_snapshot=snapshot,
			order_digest=digest,
			material_snapshot=original.source_snapshot,
			material_digest=original.source_snapshot_digest,
			amount=amount,
			reason="Synthetic capacity review",
		)
		inspection = PurchaseUsageInspection(
			source, original.budget_date, (), positions, positions, (), keys, (source,), discovered
		)
		return inspection, purchase, proposed, pairs

	def check_quota(self, inspection, purchase, proposed):
		with (
			patch.object(
				allocations, "locked_purchase_order_source", return_value=(inspection.source, purchase)
			),
			patch.object(
				material_usage_sources, "locked_material_request_source", return_value=inspection.source
			),
			patch.object(observation, "locked_material_request_source", return_value=inspection.source),
		):
			return allocations.verify_guarded_commitment_capacity(inspection, purchase, proposed)

	def test_global_purchase_capacity_counts_two_independently_authorized_budget_roots(self):
		inspection, purchase, proposed, _ = self.quota_fixture()
		result = self.check_quota(inspection, purchase, proposed)
		self.assertEqual(
			(result.committed, result.remaining, result.decision_count), ("8.000000000", "4.000000000", 2)
		)
		self.assertNotIn("decision_set_digest", repr(result))
		assert_budget_guards(inspection.keys)

	def test_purchase_capacity_overflow_poisons_guard_even_after_savepoint_rollback(self):
		inspection, purchase, proposed, _ = self.quota_fixture(amount="4.01")
		frappe.db.savepoint("before_quota_failure")
		with self.assertRaises(IoneApplicationError):
			self.check_quota(inspection, purchase, proposed)
		frappe.db.rollback(save_point="before_quota_failure")
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards(inspection.keys)

	def test_global_purchase_capacity_does_not_ignore_allocation_outside_guarded_budget(self):
		inspection, purchase, proposed, _ = self.quota_fixture(guard_all=False)
		with self.assertRaises(IoneApplicationError):
			self.check_quota(inspection, purchase, proposed)

	def test_purchase_capacity_separates_other_lines_on_same_order(self):
		inspection, purchase, proposed, _ = self.quota_fixture(other_line=True)
		result = self.check_quota(inspection, purchase, proposed)
		self.assertEqual(
			(result.decision_count, result.committed, result.remaining), (2, "8.000000000", "4.000000000")
		)

	def test_purchase_capacity_rejects_changed_economic_facts_even_with_free_capacity(self):
		inspection, purchase, proposed, _ = self.quota_fixture(
			amount="1.00",
			purchase_changes={"rate": "5.500000000", "amount": "11.000000000", "base_amount": "11.000000000"},
		)
		with self.assertRaises(IoneApplicationError):
			self.check_quota(inspection, purchase, proposed)

	def test_purchase_capacity_requires_prebound_purchase_parent(self):
		inspection, purchase, proposed, _ = self.quota_fixture(prebind=False)
		with self.assertRaises(IoneApplicationError):
			self.check_quota(inspection, purchase, proposed)

	def test_purchase_capacity_stable_identity_query_detects_hidden_parent_mutation(self):
		inspection, purchase, proposed, pairs = self.quota_fixture()
		frappe.db.sql(
			"UPDATE `tabHRP Budget Commitment Authorization` SET purchase_order=%s WHERE name=%s",
			("OTHER-ORDER", pairs[1][1]["name"]),
		)
		with self.assertRaises(IoneApplicationError):
			self.check_quota(inspection, purchase, proposed)

	def test_purchase_capacity_checks_decimal_totals_without_float_tolerance(self):
		inspection, purchase, proposed, _ = self.quota_fixture(
			amount="0.01",
			commits=("0.10", "0.20"),
			order_changes={"quantity": "0.050000000", "amount": "0.300000000", "base_amount": "0.300000000"},
		)
		with self.assertRaises(IoneApplicationError):
			self.check_quota(inspection, purchase, proposed)

	def test_purchase_capacity_bounded_inventory_does_not_return_partial_totals(self):
		inspection, purchase, proposed, _ = self.quota_fixture()
		with patch.object(allocations, "MAX_ORDER_DECISIONS", 1), self.assertRaises(IoneApplicationError):
			self.check_quota(inspection, purchase, proposed)

	def insert_initial_root(self, *, index=1, source=None, key=None):
		source, original, _, events, first, second = synthetic_rows(index=index, source=source, key=key)
		documents = initial_reservation_documents(first, second, occurred_at=events[0]["occurred_at"])
		root = {field: documents.root[field] for field in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}}
		for table, row in (
			("tabHRP Budget Reservation", root),
			("tabHRP Budget Reservation Event", dict(documents.event)),
			("tabHRP Budget Reservation Authorization", first),
			("tabHRP Budget Reservation Authorization", second),
		):
			self.insert_row(table, row)
		return source, original, root

	def historical_version_fixture(self, *, economic=False):
		old, original, _ = self.insert_initial_root()
		changes = {"status": "Partially Ordered", "request_modified": "2026-01-02 09:00:00"}
		if economic:
			changes.update(rate="7.000000000", amount="35.000000000")
		current = replace(old, **changes)
		self.insert_initial_root(index=2, source=current)
		with self.assertRaises(IoneApplicationError):
			material_usage_sources.discover_material_usage_sources([original.key])
		discovered = material_usage_sources.discover_material_usage_sources([original.key], historical=True)
		self.assertEqual(len(discovered), 1)
		self.bind_release_source(current, original)
		with patch.object(material_usage_sources, "locked_material_request_source", return_value=current):
			positions = material_usage_sources.guarded_verify_provisional_material_usage_sources(
				[original.key], (current,), discovered=discovered
			)
		return current, original, positions

	def test_historical_discovery_checks_each_nonfinancial_source_version_independently(self):
		current, original, positions = self.historical_version_fixture()
		with patch.object(observation, "locked_material_request_source", return_value=current):
			checked = observation.verify_guarded_initial_authorizations([original.key], (current,), positions)
		self.assertEqual(len(checked[(current.request, current.line, current.organization_unit)]), 2)
		assert_budget_guards([original.key])

	def test_historical_discovery_does_not_authorize_economic_source_changes(self):
		current, original, positions = self.historical_version_fixture(economic=True)
		with (
			patch.object(observation, "locked_material_request_source", return_value=current),
			self.assertRaises(IoneApplicationError),
		):
			observation.verify_guarded_initial_authorizations([original.key], (current,), positions)
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([original.key])

	def discover_scope_fixture(self, *, extra_source=False, alias=False, bound=None):
		source, original, _ = self.insert_initial_root()
		key = replace(original.key, funding_source="Other Fund")
		if extra_source:
			key = original.key
		second_source = replace(source, request="OTHER-MR", line="OTHER-LINE") if extra_source else source
		self.insert_initial_root(index=2, source=second_source, key=key)
		calls = []

		def native(doctype, name, fields, *, lock=False):
			calls.append((doctype, name, lock))
			return SimpleNamespace(name=name, company=source.company, hospital=source.hospital)

		def resolve(value):
			key = budget_control_key(value)
			return replace(key, funding_source="Canonical Alias") if alias else key

		with (
			patch.object(commitment_state, "native_document", side_effect=native),
			patch.object(commitment_state, "resolve_budget_control_key", side_effect=resolve),
			patch.object(commitment_state, "_actor"),
			patch.object(commitment_state, "MAX_BOUND_MATERIAL_SOURCES", bound or 100),
		):
			keys = commitment_state.discover_commitment_scope(
				original, "PO-001", "PO-LINE-001", "Administrator"
			)
		return original, keys, calls

	def test_commitment_scope_closure_discovers_other_budget_for_same_source_without_locks(self):
		original, keys, calls = self.discover_scope_fixture()
		self.assertEqual({key.funding_source for key in keys}, {original.key.funding_source, "Other Fund"})
		self.assertTrue(calls and all(not lock for _, _, lock in calls))
		self.assertIsNone(getattr(frappe.local.db, "_medops_budget_guard_marker", None))

	def test_commitment_scope_closure_rejects_aliases_and_source_overflow(self):
		with self.assertRaises(ValueError):
			self.discover_scope_fixture(alias=True)
		frappe.db.rollback()
		with self.assertRaises(ValueError):
			self.discover_scope_fixture(extra_source=True, bound=1)
		self.assertIsNone(getattr(frappe.local.db, "_medops_budget_guard_marker", None))

	def test_commitment_prepare_cannot_upgrade_an_existing_budget_guard(self):
		source, original, root = self.insert_initial_root()
		self.bind_release_source(source, original)
		with patch.object(commitment_state, "_actor"), self.assertRaises(IoneApplicationError) as denied:
			commitment_state.inspect_reservation_commitment(
				root["name"], "PO-001", "PO-LINE-001", amount="1.00", reason="Synthetic"
			)
		self.assertEqual(denied.exception.definition.key, "OPERATION_NOT_ALLOWED")
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([original.key])
