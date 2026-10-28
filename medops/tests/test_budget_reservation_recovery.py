"""Actual temporary evidence storage; source/guards/lineage are controlled doubles.

The separately quarantined committed probe exercises the real HTTP and history.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import set_request

from medops.common.error_catalog import IoneApplicationError
from medops.common.reservation_authorization import verify_reservation_authorization_rows
from medops.common.reservation_recovery import build_reservation_recovery
from medops.hrp_budget.services import reservation_recovery as recovery
from medops.services.errors import raise_ione_error
from medops.tests.test_budget_material_authorization_observation import synthetic_rows


class TestBudgetReservationRecovery(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.rollback()
		self.before_request = getattr(frappe.local, "request", None)
		self.before_sid = frappe.session.sid
		set_request(method="POST", path="/api/method/medops.api.v1.finance.reservation_operations.recovery")
		frappe.session.sid = "synthetic-recovery-session"
		self.source, self.item, _, _, self.first, self.second = synthetic_rows()
		_, self.original = verify_reservation_authorization_rows(self.first, self.second)
		self.command = build_reservation_recovery(
			{
				"source_snapshot": self.item.source_snapshot,
				"source_snapshot_digest": self.item.source_snapshot_digest,
				"budget_key": asdict(self.item.key),
			}
		)
		self.addCleanup(
			lambda: frappe.db.sql("DROP TEMPORARY TABLE IF EXISTS `tabHRP Budget Reservation Authorization`")
		)
		self.addCleanup(
			lambda: frappe.db.sql("DROP TEMPORARY TABLE IF EXISTS `medops_recovery_auth_template`")
		)
		frappe.db.sql(
			"CREATE TEMPORARY TABLE `medops_recovery_auth_template` LIKE `tabHRP Budget Reservation Authorization`"
		)
		frappe.db.sql(
			"CREATE TEMPORARY TABLE `tabHRP Budget Reservation Authorization` LIKE `medops_recovery_auth_template`"
		)
		frappe.db.sql("DROP TEMPORARY TABLE `medops_recovery_auth_template`")
		for row in (self.first, self.second):
			values = dict(
				row,
				docstatus=0,
				owner="Administrator",
				creation="2026-01-01 09:00:00",
				modified="2026-01-01 09:00:00",
				modified_by="Administrator",
			)
			fields = sorted(values)
			frappe.db.sql(
				"INSERT INTO `tabHRP Budget Reservation Authorization` ("
				+ ",".join(f"`{key}`" for key in fields)
				+ ") VALUES ("
				+ ",".join(["%s"] * len(fields))
				+ ")",  # nosec B608 - fixed fixture fields/table
				tuple(values[key] for key in fields),
			)
		self.patches = ExitStack()
		self.inspection = SimpleNamespace(source=self.source, source_usage=())
		self.patches.enter_context(
			patch.object(recovery, "inspect_purchase_usage", return_value=self.inspection)
		)
		self.patches.enter_context(
			patch.object(recovery, "locked_material_request_source", return_value=self.source)
		)
		self.patches.enter_context(patch.object(recovery, "assert_material_budget_guards"))
		self.patches.enter_context(
			patch.object(
				recovery,
				"locked_renewal_lineage",
				return_value=SimpleNamespace(original=self.original, renewals=()),
			)
		)
		self.patches.enter_context(
			patch.object(recovery, "now_datetime", return_value=datetime(2026, 1, 1, 9, 6))
		)

	def tearDown(self):
		self.patches.close()
		frappe.db.rollback()
		frappe.db.sql("DROP TEMPORARY TABLE `tabHRP Budget Reservation Authorization`")
		frappe.local.request = self.before_request
		frappe.session.sid = self.before_sid
		super().tearDown()

	def read(self, command=None):
		return recovery.ReadReservationRecovery().execute(command or self.command).result

	def test_expired_decision_recovers_intent_without_any_proof_or_idempotency_write(self):
		before = frappe.db.count("HRP Service Idempotency")
		result = self.read()
		self.assertEqual(result["total_rows"], 1)
		rows = result["rows"]
		if not isinstance(rows, list):
			self.fail("recovery rows must be a list")
		row = rows[0]
		self.assertEqual(
			(row["state"], row["decision"], row["confirmation"]),
			("Expired", self.second["name"], self.first["name"]),
		)
		self.assertEqual(row["intent"]["amount"], self.item.amount)
		encoded = frappe.as_json(result)
		for private in (
			"proof_hash",
			"session_digest",
			"source_snapshot_json",
			"intent_snapshot_json",
			"one_time_key",
		):
			self.assertNotIn(private, encoded)
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)

	def test_current_permission_denial_happens_before_private_catalog_enumeration(self):
		with (
			patch.object(
				recovery,
				"inspect_purchase_usage",
				side_effect=lambda *a, **k: raise_ione_error("PERMISSION_DENIED"),
			),
			patch.object(recovery, "_names") as names,
			self.assertRaises(IoneApplicationError),
		):
			self.read()
		names.assert_not_called()

	def test_missing_or_misprojected_pair_is_rejected_before_budget_filtering(self):
		frappe.db.sql(
			"UPDATE `tabHRP Budget Reservation Authorization` SET budget_key_digest=%s WHERE name=%s",
			("f" * 64, self.second["name"]),
		)
		with self.assertRaises(IoneApplicationError):
			self.read()
		frappe.db.sql(
			"UPDATE `tabHRP Budget Reservation Authorization` SET budget_key_digest=%s WHERE name=%s",
			(self.item.key.identity, self.second["name"]),
		)
		frappe.db.sql(
			"DELETE FROM `tabHRP Budget Reservation Authorization` WHERE name=%s", (self.first["name"],)
		)
		with self.assertRaises(IoneApplicationError):
			self.read()

	def test_directory_digest_rejects_expiry_state_drift_and_final_revocation(self):
		first = self.read()
		command = build_reservation_recovery(
			{
				"source_snapshot": self.item.source_snapshot,
				"source_snapshot_digest": self.item.source_snapshot_digest,
				"budget_key": asdict(self.item.key),
				"expected_digest": first["directory_digest"],
			}
		)
		with (
			patch.object(recovery, "now_datetime", return_value=datetime(2026, 1, 1, 9, 4)),
			self.assertRaises(IoneApplicationError),
		):
			self.read(command)
		original = recovery._access
		calls = 0

		def revoke(command, source):
			nonlocal calls
			calls += 1
			if calls == 2:
				raise_ione_error("PERMISSION_DENIED")
			return original(command, source)

		with patch.object(recovery, "_access", new=revoke), self.assertRaises(IoneApplicationError):
			self.read()
