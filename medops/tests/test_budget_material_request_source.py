"""Native ERPNext source facts and exact MariaDB decimal reads."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_bench_path, set_request

from medops.common.budget_control import budget_control_key, budget_guard_names
from medops.common.budget_control_history import ReleaseInventoryEntry
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS
from medops.common.budget_reservation_storage import ROW_FIELDS, reservation_record_from_row
from medops.common.budget_workflow import build_human_confirmation
from medops.common.domain_service import idempotency_record_name
from medops.common.error_catalog import IoneApplicationError
from medops.common.initial_reservation_posting import initial_reservation_documents
from medops.common.material_source_snapshot import compare_material_source_history, material_source_snapshot
from medops.common.reservation_authorization import (
	AUTHORIZATION_ROW_FIELDS,
	reservation_intent,
	verify_reservation_authorization_rows,
)
from medops.common.reservation_commitment import reservation_commitment_intent
from medops.common.reservation_renewal import RENEWAL_ROW_FIELDS
from medops.hrp_budget.services import (
	material_budget_guards,
	material_request_source,
	material_usage_preview,
	purchase_order_source,
	release_execution,
	reservation_confirmation,
	reservation_confirmation_service,
)
from medops.hrp_budget.services.control_guards import acquire_budget_guards, assert_budget_guards
from medops.hrp_budget.services.material_budget_guards import acquire_material_budget_guards
from medops.tests import test_budget_consolidation as source_fixture
from medops.tests import test_budget_material_authorization_observation as authorization_fixture
from medops.tests.test_budget_control_guards import synthetic_key, wait_for_file

INVERTED_LOCK_CHILD = r"""
import json, sys, time
from pathlib import Path
import frappe
from medops.common.budget_control import budget_control_key
from medops.hrp_budget.services.control_guards import acquire_budget_guards, assert_budget_guards
from medops.hrp_budget.services.material_request_source import locked_material_request_source

site, request, line, unit, key_json, mode, directory = sys.argv[1:]
root = Path(directory)
connected = False
result = {'status': 'unexpected'}
def wait_for(name):
    deadline = time.monotonic() + 35
    while not (root / name).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('synthetic coordination timed out')
        time.sleep(0.05)
try:
    frappe.init(site=site, sites_path='.')
    frappe.connect()
    connected = True
    frappe.set_user('Administrator')
    frappe.db.rollback()
    frappe.db.sql('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
    frappe.db.sql('SET SESSION innodb_lock_wait_timeout = 8')
    frappe.db.begin()
    key = budget_control_key(json.loads(key_json))
    if mode == 'source-first':
        locked_material_request_source(request, line, unit)
        (root / 'source-locked').write_text('ready', encoding='utf-8')
        wait_for('guard-locked')
        (root / 'source-waiting').write_text('ready', encoding='utf-8')
        acquire_budget_guards([key])
    else:
        acquire_budget_guards([key])
        (root / 'guard-locked').write_text('ready', encoding='utf-8')
        wait_for('source-waiting')
        # Deliberately bypass the cooperating source-reader ordering check to
        # demonstrate the underlying database risk of acquiring a raw lock late.
        frappe.db.get_value('Material Request', request, 'name', for_update=True)
    assert_budget_guards([key])
    result = {'status': 'success'}
except frappe.QueryDeadlockError:
    result = {'status': 'deadlock'}
except frappe.QueryTimeoutError:
    result = {'status': 'timeout'}
except Exception as error:
    result = {'status': 'unexpected', 'type': type(error).__name__}
finally:
    if connected:
        try:
            frappe.db.rollback()
            result['guard_marker_cleared'] = getattr(frappe.local.db, '_medops_budget_guard_marker', None) is None
        except Exception as error:
            result = {'status': 'rollback_failed', 'type': type(error).__name__}
    frappe.destroy()
    (root / (mode + '-result')).write_text(json.dumps(result), encoding='utf-8')
"""


class TestBudgetMaterialRequestSource(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.organization, cls.units, _, _ = source_fixture.fixtures()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		self.created_root = not frappe.db.exists("Item Group", "All Item Groups")
		self.created_group = not frappe.db.exists("Item Group", "COD034 Products")
		self.created_uom = not frappe.db.exists("UOM", "COD034 Unit")
		self.created_item = not frappe.db.exists("Item", "COD034-SOURCE-ITEM")
		if self.created_root:
			frappe.get_doc(dict(doctype="Item Group", item_group_name="All Item Groups", is_group=1)).insert(
				ignore_permissions=True
			)
		if self.created_group:
			frappe.get_doc(
				dict(
					doctype="Item Group",
					item_group_name="COD034 Products",
					parent_item_group="All Item Groups",
				)
			).insert(ignore_permissions=True)
		if self.created_uom:
			frappe.get_doc(dict(doctype="UOM", uom_name="COD034 Unit")).insert(ignore_permissions=True)
		if self.created_item:
			frappe.get_doc(
				dict(
					doctype="Item",
					item_code="COD034-SOURCE-ITEM",
					item_name="COD034 source fixture",
					item_group="COD034 Products",
					stock_uom="COD034 Unit",
					is_stock_item=0,
				)
			).insert(ignore_permissions=True)
		self.cost_center = frappe.db.get_value("HRP Organization Mapping", self.units["A"], "cost_center")
		self.account = frappe.db.get_value(
			"Account",
			{
				"company": source_fixture.COMPANY,
				"is_group": 0,
				"disabled": 0,
				"report_type": "Profit and Loss",
			},
			"name",
		)
		self.assertTrue(self.account)
		warehouse = frappe.db.get_value(
			"Warehouse", {"company": source_fixture.COMPANY, "is_group": 0}, "name"
		)
		self.request = frappe.get_doc(
			dict(
				doctype="Material Request",
				company=source_fixture.COMPANY,
				material_request_type="Purchase",
				transaction_date="2026-10-26",
				schedule_date="2026-10-27",
				items=[
					dict(
						item_code="COD034-SOURCE-ITEM",
						qty=1,
						uom="COD034 Unit",
						conversion_factor=1,
						warehouse=warehouse,
						cost_center=self.cost_center,
						expense_account=self.account,
						schedule_date="2026-10-27",
					)
				],
			)
		).insert(ignore_permissions=True)
		self.line = self.request.items[0].name

	def test_private_reservation_confirmation_persists_exact_append_only_evidence(self):
		"""Real documents and MariaDB rows; source inspection is a test substitute."""
		self.request.items[0].rate = 10
		self.request.items[0].amount = 10
		self.request.save(ignore_permissions=True)
		self.request.submit()
		source = material_request_source.locked_material_request_source(
			self.request.name, self.line, self.units["A"]
		)
		snapshot, digest = material_source_snapshot(asdict(source))
		values = synthetic_key(uuid4().hex)
		values.update(
			company=source.company,
			hospital=source.hospital,
			organization_unit=source.organization_unit,
			project=source.project,
			period_start="2026-01-01",
			period_end="2026-12-31",
		)
		item = reservation_intent(
			source_snapshot=snapshot,
			source_snapshot_digest=digest,
			allocation="test-allocation",
			key=budget_control_key(values),
			currency=source.currency,
			precision=2,
			amount="10.00",
			budget_date="2026-10-26",
			reason="Synthetic test",
		)
		doctype = reservation_confirmation.DOCTYPE
		before = frappe.db.count(doctype)
		with (
			patch.object(reservation_confirmation, "require_web_session"),
			patch.object(reservation_confirmation, "_session", return_value="a" * 64),
			patch.object(reservation_confirmation, "_inspect_intent", return_value=source),
			patch.object(reservation_confirmation, "_recheck"),
		):
			prepared = reservation_confirmation.prepare_reservation_confirmation(item)
			with self.assertRaises(IoneApplicationError) as denied:
				reservation_confirmation.confirm_reservation_confirmation(
					prepared["confirmation"], "wrong_confirmation_proof_1234567890", True
				)
			self.assertEqual(denied.exception.code, "IONE-CORE-0002")
			self.assertEqual(frappe.db.count(doctype), before + 1)
			result = reservation_confirmation.confirm_reservation_confirmation(
				prepared["confirmation"], prepared["proof"], True
			)
			self.assertEqual(frappe.db.count(doctype), before + 2)
			first = frappe.get_doc(doctype, prepared["confirmation"])
			second = frappe.get_doc(doctype, result["decision"])
			first_row = {field: first.get(field) for field in AUTHORIZATION_ROW_FIELDS}
			second_row = {field: second.get(field) for field in AUTHORIZATION_ROW_FIELDS}
			self.assertEqual(verify_reservation_authorization_rows(first_row, second_row)[0], item)
			with self.assertRaises(IoneApplicationError):
				second.db_set("acknowledged", 0)
			with self.assertRaises(IoneApplicationError):
				reservation_confirmation.confirm_reservation_confirmation(
					prepared["confirmation"], prepared["proof"], True
				)
			self.assertEqual(frappe.db.count(doctype), before + 2)
			another = reservation_confirmation.prepare_reservation_confirmation(item)
			with self.assertRaises(IoneApplicationError):
				reservation_confirmation.confirm_reservation_confirmation(
					another["confirmation"], another["proof"], True
				)
			self.assertEqual(frappe.db.count(doctype), before + 3)

	def test_reservation_preview_uses_real_session_source_and_budget_guard(self):
		"""Native source and evidence with synthetic session and mocked release inventory."""
		self.request.items[0].rate = 10
		self.request.items[0].amount = 10
		self.request.save(ignore_permissions=True)
		self.request.submit()
		with self.committed_source_fixture():
			primary = frappe.local.db
			previous_request = getattr(frappe.local, "request", None)
			previous_sid = frappe.session.sid
			try:
				source = material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				)
				primary.rollback()
				snapshot, digest = material_source_snapshot(asdict(source))
				if source.expense_account is None:
					raise AssertionError("synthetic source expense account required")
				values = synthetic_key(uuid4().hex)
				values.update(
					company=source.company,
					hospital=source.hospital,
					organization_unit=source.organization_unit,
					project=source.project,
					period_start="2026-01-01",
					period_end="2026-12-31",
				)
				key = budget_control_key(values)
				item = reservation_intent(
					source_snapshot=snapshot,
					source_snapshot_digest=digest,
					allocation="test-allocation",
					key=key,
					currency=source.currency,
					precision=2,
					amount="10.00",
					budget_date="2026-10-26",
					reason="Synthetic test",
				)
				entry = ReleaseInventoryEntry(
					"release-row",
					key,
					source.currency,
					2,
					"20.00",
					source.expense_account,
					source.cost_center,
				)
				for method, headers, sid in (
					("GET", {}, "synthetic-test-web-session"),
					("POST", {"Authorization": "token synthetic:synthetic"}, "synthetic-test-web-session"),
					("POST", {}, "Guest"),
				):
					set_request(method=method, path="/", headers=headers)
					frappe.session.sid = sid
					with self.assertRaises(IoneApplicationError):
						reservation_confirmation.prepare_reservation_confirmation(item)
					self.assertIsNone(getattr(primary, "_medops_budget_guard_marker", None))
				set_request(method="POST", path="/")
				frappe.session.sid = "synthetic-test-web-session"
				with (
					patch.object(material_usage_preview, "resolve_budget_control_key", return_value=key),
					patch.object(material_usage_preview, "discover_material_usage_sources", return_value=()),
					patch.object(
						material_usage_preview,
						"guarded_verify_provisional_material_usage_sources",
						return_value=(),
					),
					patch.object(material_usage_preview, "guarded_release_inventory", return_value=(entry,)),
				):
					result = reservation_confirmation.prepare_reservation_confirmation(item)
					self.assertTrue(
						frappe.db.exists(reservation_confirmation.DOCTYPE, result["confirmation"])
					)
					assert_budget_guards([key])
				primary.rollback()
				self.assertFalse(frappe.db.exists(reservation_confirmation.DOCTYPE, result["confirmation"]))
			finally:
				frappe.local.db = primary
				primary.rollback()
				frappe.local.request = previous_request
				frappe.session.sid = previous_sid
				frappe.get_doc("Material Request", self.request.name).cancel()
				primary.commit()

	def test_authority_refresh_preserves_web_session_and_request_form(self):
		original_request = getattr(frappe.local, "request", None)
		original_sid, original_data, original_form = (
			frappe.session.sid,
			frappe.session.data,
			frappe.local.form_dict,
		)
		try:
			set_request(method="POST", path="/")
			frappe.session.sid = "synthetic-test-web-session"
			frappe.session.data = frappe._dict(synthetic_session_value="preserve")
			frappe.local.form_dict = frappe._dict(payload="synthetic-proof-carrier")
			session_data, form = frappe.session.data, frappe.local.form_dict
			release_execution.refresh_financial_authority("Administrator")
			self.assertEqual(frappe.session.user, "Administrator")
			self.assertEqual(frappe.session.sid, "synthetic-test-web-session")
			self.assertIs(frappe.session.data, session_data)
			self.assertIs(frappe.local.form_dict, form)
			with self.assertRaises(IoneApplicationError):
				release_execution.refresh_financial_authority("other-synthetic-user")
			self.assertEqual(frappe.session.user, "Administrator")
			self.assertEqual(frappe.session.sid, "synthetic-test-web-session")
			self.assertIs(frappe.local.form_dict, form)
		finally:
			frappe.local.request = original_request
			frappe.session.sid, frappe.session.data, frappe.local.form_dict = (
				original_sid,
				original_data,
				original_form,
			)

	def test_authority_refresh_allows_verified_background_actor_switch(self):
		"""A worker may adopt the separately verified synthetic decision actor."""
		previous_request = getattr(frappe.local, "request", None)
		try:
			frappe.local.request = None
			frappe.set_user("Administrator")
			release_execution.refresh_financial_authority(source_fixture.MANAGER)
			self.assertEqual(frappe.session.user, source_fixture.MANAGER)
			self.assertEqual(frappe.session.sid, source_fixture.MANAGER)
			self.assertIn("HRP Budget Manager", frappe.get_roles())
		finally:
			frappe.set_user("Administrator")
			frappe.local.request = previous_request

	def test_reservation_domain_services_replay_and_protect_confirmation_proof(self):
		"""Exercise encrypted receipts and evidence rows; source/session are substituted."""
		self.request.items[0].rate = 10
		self.request.items[0].amount = 10
		self.request.save(ignore_permissions=True)
		self.request.submit()
		source = material_request_source.locked_material_request_source(
			self.request.name, self.line, self.units["A"]
		)
		snapshot, digest = material_source_snapshot(asdict(source))
		values = synthetic_key(uuid4().hex)
		values.update(
			company=source.company,
			hospital=source.hospital,
			organization_unit=source.organization_unit,
			project=source.project,
			period_start="2026-01-01",
			period_end="2026-12-31",
		)
		item = reservation_intent(
			source_snapshot=snapshot,
			source_snapshot_digest=digest,
			allocation="test-allocation",
			key=budget_control_key(values),
			currency=source.currency,
			precision=2,
			amount="10.00",
			budget_date="2026-10-26",
			reason="Synthetic test",
		)
		preview_key, confirm_key = "preview-" + uuid4().hex, "confirm-" + uuid4().hex
		with (
			patch.object(reservation_confirmation, "require_web_session"),
			patch.object(reservation_confirmation, "_session", return_value="a" * 64),
			patch.object(reservation_confirmation, "_inspect_intent", return_value=source),
			patch.object(reservation_confirmation, "_recheck"),
			patch.object(reservation_confirmation_service, "require_web_session"),
			patch.object(reservation_confirmation_service, "session_digest", return_value="a" * 64),
		):
			service = reservation_confirmation_service.PrepareReservationConfirmation
			first = service().execute(item, idempotency_key=preview_key)
			replay = service().execute(item, idempotency_key=preview_key)
			self.assertFalse(first.idempotency_replayed)
			self.assertTrue(replay.idempotency_replayed)
			self.assertEqual(first.result, replay.result)
			proof = first.result["proof"]
			receipt_name = idempotency_record_name(service.definition.name, preview_key)
			receipt = frappe.get_doc("HRP Service Idempotency", receipt_name)
			self.assertNotIn(proof, receipt.response_snapshot)
			with patch.object(reservation_confirmation_service, "session_digest", return_value="b" * 64):
				with self.assertRaises(IoneApplicationError):
					service().execute(item, idempotency_key=preview_key)
			with patch.object(
				reservation_confirmation_service, "session_digest", side_effect=["a" * 64, "b" * 64]
			):
				with self.assertRaises(IoneApplicationError):
					service().execute(item, idempotency_key=preview_key)
			expiry = datetime.fromisoformat(str(first.result["expires_at"]))
			with patch.object(
				reservation_confirmation_service,
				"now_datetime",
				side_effect=[expiry - timedelta(seconds=1), expiry],
			):
				with self.assertRaises(IoneApplicationError):
					service().execute(item, idempotency_key=preview_key)
			command = build_human_confirmation(
				{"confirmation": first.result["confirmation"], "proof": proof, "acknowledge": True}
			)
			confirm = reservation_confirmation_service.ConfirmReservationDecision
			decision = confirm().execute(command, idempotency_key=confirm_key)
			self.assertFalse(decision.idempotency_replayed)
			future = reservation_confirmation_service.now_datetime() + timedelta(days=1)
			with patch.object(reservation_confirmation_service, "now_datetime", return_value=future):
				with self.assertRaises(IoneApplicationError):
					service().execute(item, idempotency_key=preview_key)
				repeated = confirm().execute(command, idempotency_key=confirm_key)
			self.assertTrue(repeated.idempotency_replayed)
			self.assertEqual(decision.result, repeated.result)

	@contextmanager
	def committed_source_fixture(self):
		"""Publish only synthetic draft fixtures and remove the ones we created."""
		primary = frappe.local.db
		primary.commit()
		try:
			# The pinned lazy secondary connector changes frappe.local.db while
			# creating the connection. Restore the original explicitly.
			try:
				with self.secondary_connection():
					self._secondary_connection.rollback()
			finally:
				frappe.local.db = primary
			primary.rollback()
			primary.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
			primary.begin()
			yield
		finally:
			frappe.local.db = primary
			frappe.set_user("Administrator")
			primary.rollback()
			for doctype, name, owned in (
				("Material Request", self.request.name, True),
				("Item", "COD034-SOURCE-ITEM", self.created_item),
				("Item Group", "COD034 Products", self.created_group),
				("UOM", "COD034 Unit", self.created_uom),
				("Item Group", "All Item Groups", self.created_root),
			):
				if owned and frappe.db.exists(doctype, name):
					frappe.delete_doc(doctype, name, ignore_permissions=True)
			primary.commit()
			primary.begin()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.begin()
		super().tearDown()

	def test_locked_source_exact_amount_mapping_permissions_and_child_change(self):
		# The fixture is a draft ERPNext document; no submitted document or ledger
		# is changed. Setting a string directly demonstrates the DECIMAL driver
		# boundary even when the equivalent Python float cannot preserve it.
		frappe.db.set_value("Material Request Item", self.line, "amount", "999999999999.123456789")
		before = material_request_source.locked_material_request_source(
			self.request.name, self.line, self.units["A"]
		)
		self.assertEqual(before.amount, "999999999999.123456789")
		self.assertEqual(
			(before.company, before.organization_unit, before.cost_center),
			(source_fixture.COMPANY, self.units["A"], self.cost_center),
		)
		self.assertEqual((before.currency, before.docstatus), ("CNY", 0))
		self.assertEqual(before.expense_account, self.account)
		self.assertEqual(len(before.digest()), 64)
		with self.assertRaises(IoneApplicationError):
			material_request_source.locked_material_request_source(
				self.request.name, self.line, self.units["B"]
			)
		with patch.object(material_request_source, "native_read", return_value=False):
			with self.assertRaises(IoneApplicationError):
				material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				)
		group = frappe.db.get_value("Account", {"company": source_fixture.COMPANY, "is_group": 1}, "name")
		self.assertTrue(group)
		frappe.db.set_value("Material Request Item", self.line, "expense_account", group)
		with self.assertRaises(IoneApplicationError):
			material_request_source.locked_material_request_source(
				self.request.name, self.line, self.units["A"]
			)
		frappe.db.set_value("Material Request Item", self.line, "expense_account", self.account)
		frappe.db.set_value("Material Request Item", self.line, "amount", "999999999999.123456788")
		after = material_request_source.locked_material_request_source(
			self.request.name, self.line, self.units["A"]
		)
		self.assertEqual(after.amount, "999999999999.123456788")
		self.assertNotEqual(before.digest(), after.digest())
		frappe.set_user("Guest")
		with self.assertRaises(IoneApplicationError):
			material_request_source.locked_material_request_source(
				self.request.name, self.line, self.units["A"]
			)

	def test_other_transaction_cannot_change_locked_parent_or_child(self):
		with self.committed_source_fixture():
			primary = frappe.local.db
			before = material_request_source.locked_material_request_source(
				self.request.name, self.line, self.units["A"]
			)
			with self.secondary_connection():
				other = frappe.local.db
				other.rollback()
				other.sql("SET SESSION innodb_lock_wait_timeout = 1")
				for doctype, name, field, value in (
					("Material Request", self.request.name, "title", "concurrent change"),
					("Material Request Item", self.line, "amount", "10.000000000"),
				):
					with self.subTest(doctype=doctype), self.assertRaises(frappe.QueryTimeoutError):
						other.set_value(doctype, name, field, value)
					other.rollback()
			self.assertIs(frappe.local.db, primary)
			self.assertEqual(
				material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				).digest(),
				before.digest(),
			)
			primary.rollback()
			with self.secondary_connection():
				other = frappe.local.db
				other.set_value("Material Request Item", self.line, "amount", "10.000000000")
				other.rollback()

	def test_two_real_purchase_sources_are_locked_before_shared_budget_guard(self):
		second = frappe.copy_doc(self.request).insert(ignore_permissions=True)
		with self.committed_source_fixture():
			primary = frappe.local.db
			try:
				first_source = material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				)
				second_source = material_request_source.locked_material_request_source(
					second.name, second.items[0].name, self.units["A"]
				)
				primary.rollback()
				values = synthetic_key(uuid4().hex)
				values.update(
					company=first_source.company,
					hospital=first_source.hospital,
					organization_unit=first_source.organization_unit,
					project=first_source.project,
				)
				key = budget_control_key(values)
				references = [
					material_budget_guards.MaterialSourceReference(
						source.request, source.line, source.organization_unit, source.digest()
					)
					for source in (second_source, first_source)
				]
				locked = material_budget_guards.acquire_material_budget_guards_for_sources(references, [key])
				self.assertEqual({item.request for item in locked}, {self.request.name, second.name})
				with self.secondary_connection():
					other = frappe.local.db
					other.rollback()
					other.sql("SET SESSION innodb_lock_wait_timeout = 1")
					for name in (self.request.name, second.name):
						with self.subTest(request=name), self.assertRaises(frappe.QueryTimeoutError):
							other.get_value("Material Request", name, "name", for_update=True)
						other.rollback()
				self.assertIs(frappe.local.db, primary)
				assert_budget_guards([key])
			finally:
				frappe.local.db = primary
				primary.rollback()
				if frappe.db.exists("Material Request", second.name):
					frappe.delete_doc("Material Request", second.name, ignore_permissions=True)
				primary.commit()

	def test_real_purchase_source_flows_through_empty_usage_inspection(self):
		# The only changed ERPNext document is this isolated synthetic draft.
		self.request.items[0].rate = 10
		self.request.items[0].amount = 10
		self.request.save(ignore_permissions=True)
		with self.committed_source_fixture():
			primary = frappe.local.db
			tables = (
				("tabHRP Budget Reservation", ANCHOR_ROW_FIELDS),
				("tabHRP Budget Reservation Event", ROW_FIELDS),
			)
			try:
				for table, fields in tables:
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
					primary.sql(
						f"CREATE TEMPORARY TABLE `{table}` ({', '.join(columns)}) ENGINE=InnoDB "
						"DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"  # nosec B608 - fixed test tables and fields
					)
				source = material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				)
				primary.rollback()
				values = synthetic_key(uuid4().hex)
				values.update(
					company=source.company,
					hospital=source.hospital,
					organization_unit=source.organization_unit,
					project=source.project,
					period_start="2026-10-01",
					period_end="2026-10-31",
				)
				key = budget_control_key(values)
				with (
					patch.object(material_usage_preview, "resolve_budget_control_key", return_value=key),
					patch.object(material_usage_preview, "guarded_release_inventory", return_value=()),
				):
					result = material_usage_preview.inspect_purchase_usage(
						source.request,
						source.line,
						source.organization_unit,
						expected_source_digest=source.digest(),
						budget_date="2026-10-26",
						proposed_keys=[values],
					)
				self.assertEqual((result.source, result.candidates, result.usage), (source, (), ()))
				assert_budget_guards([key])
			finally:
				frappe.local.db = primary
				primary.rollback()
				for table, _ in reversed(tables):
					primary.sql(f"DROP TEMPORARY TABLE IF EXISTS `{table}`")  # nosec B608 - fixed test tables
				primary.rollback()

	def test_real_submitted_purchase_order_is_linked_to_material_request(self):
		# All documents in this test are synthetic, and are cancelled and removed
		# after the committed fixture is used by the independent connection.
		created_group = not frappe.db.exists("Supplier Group", "All Supplier Groups")
		if created_group:
			frappe.get_doc(
				dict(doctype="Supplier Group", supplier_group_name="All Supplier Groups", is_group=1)
			).insert(ignore_permissions=True)
		supplier = frappe.get_doc(
			dict(
				doctype="Supplier",
				supplier_name="COD035 Supplier " + uuid4().hex[:12],
				supplier_group="All Supplier Groups",
				supplier_type="Company",
				country="China",
			)
		).insert(ignore_permissions=True)
		self.request.items[0].rate = 10
		self.request.items[0].amount = 10
		self.request.save(ignore_permissions=True)
		self.request.submit()
		initial_material = material_request_source.locked_material_request_source(
			self.request.name, self.line, self.units["A"]
		)
		historical_snapshot, historical_digest = material_source_snapshot(asdict(initial_material))
		self.assertEqual(historical_digest, initial_material.digest())
		_, intent, root, events, confirmation, decision = authorization_fixture.synthetic_rows(
			source=initial_material, amount="1.00"
		)
		documents = initial_reservation_documents(
			confirmation, decision, occurred_at=events[0]["occurred_at"]
		)
		root = {field: documents.root[field] for field in ANCHOR_ROW_FIELDS | {"source_snapshot_json"}}
		events = [dict(documents.event)]
		item = self.request.items[0]
		order = frappe.get_doc(
			dict(
				doctype="Purchase Order",
				supplier=supplier.name,
				company=source_fixture.COMPANY,
				transaction_date="2026-10-26",
				schedule_date="2026-10-27",
				currency="CNY",
				conversion_rate=1,
				items=[
					dict(
						item_code=item.item_code,
						qty=1,
						rate=10,
						uom=item.uom,
						stock_uom=item.uom,
						conversion_factor=1,
						warehouse=item.warehouse,
						schedule_date="2026-10-27",
						cost_center=self.cost_center,
						expense_account=self.account,
						material_request=self.request.name,
						material_request_item=item.name,
					)
				],
			)
		).insert(ignore_permissions=True)
		order.submit()
		with self.committed_source_fixture():
			primary = frappe.local.db
			created_tables = []
			previous_request = getattr(frappe.local, "request", None)
			previous_sid = frappe.session.sid
			try:
				material = material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				)
				comparison = compare_material_source_history(
					historical_snapshot, expected_digest=historical_digest, current_facts=asdict(material)
				)
				self.assertEqual(comparison.current_digest, material.digest())
				self.assertTrue(comparison.status_changed or comparison.timestamps_changed)
				primary.rollback()
				verified, purchase = purchase_order_source.locked_purchase_order_source(
					order.name,
					order.items[0].name,
					material.request,
					material.line,
					material.organization_unit,
					expected_request_digest=material.digest(),
				)
				self.assertEqual(verified, material)
				commitment_snapshot, commitment_digest = purchase.commitment_snapshot(verified)
				self.assertEqual(commitment_digest, purchase.digest())
				current_snapshot, current_digest = material_source_snapshot(asdict(verified))
				actual_intent = reservation_commitment_intent(
					intent,
					[reservation_record_from_row(events[0])],
					order_snapshot=commitment_snapshot,
					order_digest=commitment_digest,
					material_snapshot=current_snapshot,
					material_digest=current_digest,
					amount="1.00",
					reason="Synthetic real source commitment review",
				)
				self.assertEqual(actual_intent.reservation, root["name"])
				self.assertEqual(
					json.loads(commitment_snapshot)["material_request_digest"], material.digest()
				)
				self.assertEqual(
					(purchase.order, purchase.line, purchase.material_request_line),
					(order.name, order.items[0].name, self.line),
				)
				self.assertEqual(
					(purchase.currency, purchase.quantity, purchase.amount),
					("CNY", "1.000000000", "10.000000000"),
				)
				primary.rollback()
				linked_source, linked_orders = purchase_order_source.locked_linked_purchase_orders(
					material.request,
					material.line,
					material.organization_unit,
					expected_request_digest=material.digest(),
				)
				self.assertEqual((linked_source, linked_orders), (material, (purchase,)))
				primary.rollback()
				# Revisit the real submitted PO only after the common protocol has
				# prelocked it, both parents and the exact current MR snapshot.
				bound = material_budget_guards.acquire_material_budget_guards_for_sources(
					[
						material_budget_guards.MaterialSourceReference(
							material.request, material.line, material.organization_unit, material.digest()
						)
					],
					[intent.key],
					purchase_parents=(order.name,),
				)
				self.assertEqual(bound, (material,))
				revisited = purchase_order_source.locked_purchase_order_source(
					order.name,
					order.items[0].name,
					material.request,
					material.line,
					material.organization_unit,
					expected_request_digest=material.digest(),
				)
				self.assertEqual(revisited, (material, purchase))
				assert_budget_guards([intent.key])
				primary.rollback()
				# Only connection-private synthetic evidence is inserted; the real source
				# and its locks remain native. Budget resolution and release are fixtures.
				tables = (
					("tabHRP Budget Reservation", ANCHOR_ROW_FIELDS | {"source_snapshot_json"}),
					("tabHRP Budget Reservation Event", ROW_FIELDS),
					("tabHRP Budget Reservation Authorization", AUTHORIZATION_ROW_FIELDS),
					("tabHRP Budget Reservation Renewal", RENEWAL_ROW_FIELDS),
				)
				integers = {"event_count", "sequence", "currency_precision", "proof_version", "acknowledged"}
				for table, fields in tables:
					columns = []
					for name in sorted(fields):
						kind = (
							"INT"
							if name in integers
							else "LONGTEXT"
							if name.endswith("_json")
							else "VARCHAR(255)"
						)
						columns.append(f"`{name}` {kind}")
					primary.sql(
						f"CREATE TEMPORARY TABLE `{table}` ({', '.join(columns)}) ENGINE=InnoDB"  # nosec B608 - fixed test tables
					)
					created_tables.append(table)
				rows = (
					("tabHRP Budget Reservation", root),
					("tabHRP Budget Reservation Event", events[0]),
					("tabHRP Budget Reservation Authorization", confirmation),
					("tabHRP Budget Reservation Authorization", decision),
				)

				def populate_evidence():
					for table, row in rows:
						authorization_fixture.TestBudgetMaterialAuthorizationObservation.insert_row(
							self, table, row
						)

				populate_evidence()
				if not initial_material.expense_account:
					raise AssertionError("synthetic source requires an expense account")
				entry = ReleaseInventoryEntry(
					"synthetic-release",
					intent.key,
					intent.currency,
					intent.precision,
					"20.00",
					initial_material.expense_account,
					initial_material.cost_center,
				)
				with (
					patch.object(
						material_usage_preview, "resolve_budget_control_key", return_value=intent.key
					),
					patch.object(material_usage_preview, "guarded_release_inventory", return_value=(entry,)),
				):
					inspection = material_usage_preview.inspect_purchase_usage(
						material.request,
						material.line,
						material.organization_unit,
						expected_source_digest=material.digest(),
						budget_date=initial_material.transaction_date,
						proposed_keys=[asdict(intent.key)],
					)
				self.assertEqual(inspection.source, material)
				self.assertEqual(len(inspection.usage), 1)
				self.assertEqual(inspection.source_usage, inspection.usage)
				self.assertEqual(inspection.usage[0].open_reservation, "1.00")
				self.assertEqual(inspection.candidates[0].key, intent.key)
				observation = inspection.budget_observations[0]
				self.assertEqual(
					(observation.net_releases, observation.exposure, observation.difference),
					("20.00", "1.00", "19.00"),
				)
				assert_budget_guards([intent.key])
				primary.rollback()

				current_snapshot, current_digest = material_source_snapshot(asdict(material))
				set_request(method="POST", path="/")
				frappe.session.sid = "synthetic-test-web-session"
				for amount, release_amount, should_succeed in (
					("10.00", "20.00", False),
					("9.00", "20.00", True),
					("2.00", "2.00", False),
				):
					populate_evidence()
					proposed = reservation_intent(
						source_snapshot=current_snapshot,
						source_snapshot_digest=current_digest,
						allocation="next-synthetic-allocation",
						key=intent.key,
						currency=intent.currency,
						precision=intent.precision,
						amount=amount,
						budget_date=initial_material.transaction_date,
						reason="Synthetic test",
					)
					with (
						patch.object(
							material_usage_preview, "resolve_budget_control_key", return_value=intent.key
						),
						patch.object(
							material_usage_preview,
							"guarded_release_inventory",
							return_value=(replace(entry, amount=release_amount),),
						),
						patch.object(
							reservation_confirmation,
							"_append",
							return_value=SimpleNamespace(name="synthetic-preview"),
						) as append,
					):
						if should_succeed:
							result = reservation_confirmation.prepare_reservation_confirmation(proposed)
							self.assertEqual(result["confirmation"], "synthetic-preview")
							append.assert_called_once()
							assert_budget_guards([intent.key])
						else:
							with self.assertRaises(IoneApplicationError):
								reservation_confirmation.prepare_reservation_confirmation(proposed)
							append.assert_not_called()
							with self.assertRaises(IoneApplicationError):
								assert_budget_guards([intent.key])
					primary.rollback()
			finally:
				frappe.local.db = primary
				primary.rollback()
				frappe.local.request = previous_request
				frappe.session.sid = previous_sid
				for table in reversed(created_tables):
					primary.sql(f"DROP TEMPORARY TABLE `{table}`")  # nosec B608 - fixed test tables
				primary.rollback()
				if frappe.db.exists("Purchase Order", order.name):
					current = frappe.get_doc("Purchase Order", order.name)
					if current.docstatus == 1:
						current.cancel()
					frappe.delete_doc("Purchase Order", order.name, ignore_permissions=True)
				if frappe.db.exists("Material Request", self.request.name):
					current = frappe.get_doc("Material Request", self.request.name)
					if current.docstatus == 1:
						current.cancel()
				if frappe.db.exists("Supplier", supplier.name):
					frappe.delete_doc("Supplier", supplier.name, ignore_permissions=True)
				if created_group and frappe.db.exists("Supplier Group", "All Supplier Groups"):
					frappe.delete_doc("Supplier Group", "All Supplier Groups", ignore_permissions=True)
				primary.commit()

	def test_generic_budget_guard_denies_late_source_lock(self):
		guard = budget_control_key(synthetic_key(uuid4().hex))
		acquire_budget_guards([guard])
		with patch.object(material_request_source, "native_document") as read_document:
			with self.assertRaises(IoneApplicationError) as denied:
				material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				)
		self.assertEqual(denied.exception.code, "IONE-CORE-0008")
		read_document.assert_not_called()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([guard])

	def test_bound_guard_rejects_a_different_source_before_locking(self):
		source = material_request_source.locked_material_request_source(
			self.request.name, self.line, self.units["A"]
		)
		values = synthetic_key(uuid4().hex)
		values.update(
			company=source.company,
			hospital=source.hospital,
			organization_unit=source.organization_unit,
			project=source.project,
		)
		guard = budget_control_key(values)
		acquire_material_budget_guards(source, [guard])
		with patch.object(material_request_source, "native_document") as read_document:
			with self.assertRaises(IoneApplicationError) as denied:
				material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["B"]
				)
		self.assertEqual(denied.exception.code, "IONE-CORE-0008")
		read_document.assert_not_called()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([guard])

	def test_source_lock_rollback_invalidates_bound_guard_before_relocking(self):
		frappe.db.savepoint("cod034_before_material_source")
		source = material_request_source.locked_material_request_source(
			self.request.name, self.line, self.units["A"]
		)
		values = synthetic_key(uuid4().hex)
		values.update(
			company=source.company,
			hospital=source.hospital,
			organization_unit=source.organization_unit,
			project=source.project,
		)
		guard = budget_control_key(values)
		acquire_material_budget_guards(source, [guard])
		frappe.db.rollback(save_point="cod034_before_material_source")
		with patch.object(material_request_source, "native_document") as read_document:
			with self.assertRaises(IoneApplicationError) as denied:
				material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				)
		self.assertEqual(denied.exception.code, "IONE-CORE-0008")
		read_document.assert_not_called()
		with self.assertRaises(IoneApplicationError):
			assert_budget_guards([guard])

	def test_reversed_source_and_guard_lock_order_deadlocks_and_rolls_back(self):
		"""A late source lock can deadlock a transaction that locked source first."""
		values = synthetic_key(uuid4().hex)
		guard = budget_control_key(values)
		names = budget_guard_names([guard])
		processes = []
		with self.committed_source_fixture():
			primary = frappe.local.db
			before = material_request_source.locked_material_request_source(
				self.request.name, self.line, self.units["A"]
			).digest()
			primary.rollback()
			for name in names:
				self.assertEqual(
					primary.sql("SELECT COUNT(*) FROM `tabSeries` WHERE `name`=%s", (name,))[0][0], 0
				)
			with tempfile.TemporaryDirectory(prefix="medops-source-guard-order-") as directory:
				root = Path(directory)

				def launch(mode):
					process = subprocess.Popen(
						[
							sys.executable,
							"-c",
							INVERTED_LOCK_CHILD,
							frappe.local.site,
							self.request.name,
							self.line,
							self.units["A"],
							json.dumps(values),
							mode,
							directory,
						],
						cwd=os.path.join(get_bench_path(), "sites"),
						stdin=subprocess.DEVNULL,
						stdout=subprocess.DEVNULL,
						stderr=subprocess.DEVNULL,
					)
					processes.append(process)
					return process

				try:
					source_first = launch("source-first")
					wait_for_file(root / "source-locked", source_first, timeout=35)
					guard_first = launch("guard-first")
					wait_for_file(root / "guard-locked", guard_first, timeout=35)
					self.assertEqual(source_first.wait(timeout=35), 0)
					self.assertEqual(guard_first.wait(timeout=35), 0)
					outcomes = [
						json.loads((root / f"{mode}-result").read_text(encoding="utf-8"))
						for mode in ("source-first", "guard-first")
					]
					self.assertCountEqual([item["status"] for item in outcomes], ["deadlock", "success"])
					self.assertTrue(all(item.get("guard_marker_cleared") for item in outcomes))
				finally:
					for process in processes:
						if process.poll() is None:
							process.terminate()
							try:
								process.wait(timeout=5)
							except subprocess.TimeoutExpired:
								process.kill()
								process.wait(timeout=5)
			primary.rollback()
			for name in names:
				self.assertEqual(
					primary.sql("SELECT COUNT(*) FROM `tabSeries` WHERE `name`=%s", (name,))[0][0], 0
				)
			self.assertEqual(
				material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				).digest(),
				before,
			)
			primary.rollback()

	def test_role_revocation_from_another_transaction_denies_source(self):
		manager = source_fixture.MANAGER
		with self.committed_source_fixture():
			primary = frappe.local.db
			self.assertTrue(
				frappe.db.exists(
					"Has Role", {"parenttype": "User", "parent": manager, "role": "HRP Budget Manager"}
				)
			)
			had_purchase = bool(
				frappe.db.exists(
					"Has Role", {"parenttype": "User", "parent": manager, "role": "Purchase User"}
				)
			)
			user = frappe.get_doc("User", manager)
			user.add_roles("Purchase User")
			primary.commit()
			calls = []
			original = material_request_source.native_document
			try:
				frappe.set_user(manager)
				# First prove the synthetic actor can read this complete source.
				material_request_source.locked_material_request_source(
					self.request.name, self.line, self.units["A"]
				)
				primary.rollback()

				def revoke_after_item(doctype, *args, **kwargs):
					doc = original(doctype, *args, **kwargs)
					if doctype == "Item" and kwargs.get("lock") and not calls:
						calls.append(1)
						with self.secondary_connection():
							frappe.set_user("Administrator")
							try:
								frappe.get_doc("User", manager).remove_roles("HRP Budget Manager")
								self._secondary_connection.commit()
							except Exception:
								self._secondary_connection.rollback()
								raise
						frappe.set_user(manager)
					return doc

				with patch.object(material_request_source, "native_document", side_effect=revoke_after_item):
					with self.assertRaises(IoneApplicationError) as denied:
						material_request_source.locked_material_request_source(
							self.request.name, self.line, self.units["A"]
						)
				self.assertEqual(denied.exception.code, "IONE-CORE-0002")
				self.assertEqual(calls, [1])
			finally:
				frappe.local.db = primary
				primary.rollback()
				frappe.set_user("Administrator")
				user = frappe.get_doc("User", manager)
				user.add_roles("HRP Budget Manager")
				if not had_purchase:
					user.remove_roles("Purchase User")
				primary.commit()
				frappe.cache.hdel("roles", manager)
