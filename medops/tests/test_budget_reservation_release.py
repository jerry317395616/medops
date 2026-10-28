"""Release evidence migration and deny-by-default storage; no posting workflow."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.error_catalog import IoneApplicationError
from medops.common.reservation_release import RELEASE_ROW_FIELDS, verify_reservation_release_row
from medops.tests.reservation_release_fixtures import release_evidence, rows
from medops.tests.test_budget_material_authorization_observation import synthetic_rows

DOCTYPE = "HRP Budget Reservation Release"


class TestBudgetReservationRelease(IntegrationTestCase):
	def test_storage_migration_has_private_fields_unique_keys_and_denies_all_writes(self):
		before = frappe.db.count(DOCTYPE)
		doc = frappe.get_doc(dict(doctype=DOCTYPE))
		for action in (
			lambda: doc.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True),
			doc.db_insert,
			doc.db_update,
			lambda: doc.db_set("acknowledged", 1),
			doc.on_trash,
			lambda: doc.before_rename("old", "new", False),
			doc.before_cancel,
			doc.before_update_after_submit,
		):
			with self.subTest(action=action), self.assertRaises(IoneApplicationError):
				action()
		self.assertEqual(frappe.db.count(DOCTYPE), before)
		meta = frappe.get_meta(DOCTYPE)
		self.assertFalse(meta.istable or meta.issingle or meta.is_submittable or meta.allow_rename)
		self.assertEqual(meta.permissions, [])
		for field in RELEASE_ROW_FIELDS - {"name"}:
			self.assertTrue(frappe.db.has_column(DOCTYPE, field))
		for field in ("proof_hash", "session_digest", "intent_snapshot_json"):
			self.assertTrue(meta.get_field(field).hidden and int(meta.get_field(field).permlevel) == 1)
		indexes = frappe.db.sql(
			"""SELECT COLUMN_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX FROM information_schema.STATISTICS
			WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s""",
			("tabHRP Budget Reservation Release",),
			as_dict=True,
		)
		unique = {}
		for row in indexes:
			if not int(row.NON_UNIQUE):
				unique.setdefault(row.INDEX_NAME, []).append((int(row.SEQ_IN_INDEX), row.COLUMN_NAME))
		columns = {value[0][1] for value in unique.values() if len(value) == 1 and value[0][0] == 1}
		self.assertTrue({"evidence_key", "confirmation", "one_time_key", "event_digest"} <= columns)
		self.assertTrue(
			any(row.COLUMN_NAME == "reservation" and int(row.SEQ_IN_INDEX) == 1 for row in indexes)
		)

	def test_connection_private_synthetic_rows_roundtrip_and_unique_successor(self):
		"""Use a connection-private copy, never bypass the real evidence controller."""
		from medops.common.budget_reservation_storage import reservation_record_from_row

		_, original, _, event_rows, _, _ = synthetic_rows()
		records = [reservation_record_from_row(row) for row in event_rows]
		item, confirmed, decided, _ = release_evidence(original, records)
		confirmation_row, decision_row = rows(item, confirmed, decided)
		before = frappe.db.count(DOCTYPE)
		table = "medops_release_evidence_probe"
		frappe.db.sql(
			"CREATE TEMPORARY TABLE `medops_release_evidence_probe` LIKE `tabHRP Budget Reservation Release`"
		)
		try:
			fields = tuple(sorted(RELEASE_ROW_FIELDS))
			columns = ", ".join(f"`{field}`" for field in fields)
			placeholders = ", ".join(["%s"] * len(fields))
			for row in (confirmation_row, decision_row):
				frappe.db.sql(
					f"INSERT INTO `{table}` ({columns}) VALUES ({placeholders})",  # nosec B608 - fixed table/fields; bound values
					tuple(row[field] for field in fields),
				)
			stored = frappe.db.sql(
				f"SELECT {columns} FROM `{table}` ORDER BY `recorded_at`",
				as_dict=True,  # nosec B608 - fixed identifiers
			)
			checked, first = verify_reservation_release_row(stored[0])
			self.assertEqual(checked, item)
			checked, second = verify_reservation_release_row(stored[1], first)
			self.assertEqual((checked, second), (item, decided))
			with self.assertRaises(Exception) as duplicate:
				frappe.db.sql(
					f"INSERT INTO `{table}` ({columns}) VALUES ({placeholders})",  # nosec B608 - fixed table/fields; bound values
					tuple(
						(
							decision_row
							| {
								"name": "another",
								"evidence_key": "another",
								"confirmation": "another-confirmation",
								"event_digest": "a" * 64,
							}
						)[field]
						for field in fields
					),
				)
			self.assertTrue(frappe.db.is_unique_key_violation(duplicate.exception))
		finally:
			# End the synthetic test transaction before Frappe's guarded DDL path.
			frappe.db.rollback()
			frappe.db.sql("DROP TEMPORARY TABLE IF EXISTS `medops_release_evidence_probe`")
		self.assertEqual(frappe.db.count(DOCTYPE), before)
