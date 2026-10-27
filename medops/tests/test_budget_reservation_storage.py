"""Native migration and denial checks for inactive reservation event storage."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from medops.common.error_catalog import IoneApplicationError

DOCTYPE = "HRP Budget Reservation Event"


class TestBudgetReservationStorage(FrappeTestCase):
	def test_standard_document_writes_are_closed_even_with_permission_bypass(self):
		before = frappe.db.count(DOCTYPE)
		doc = frappe.get_doc(dict(doctype=DOCTYPE))
		for action in (
			lambda: doc.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True),
			doc.db_insert,
			doc.db_update,
			lambda: doc.db_set("amount", "1.00"),
			doc.on_trash,
			lambda: doc.before_rename("old", "new", False),
		):
			with self.subTest(action=action), self.assertRaises(IoneApplicationError):
				action()
		self.assertEqual(frappe.db.count(DOCTYPE), before)

	def test_unique_event_sequence_and_source_keys_exist_in_database(self):
		meta = frappe.get_meta(DOCTYPE)
		self.assertFalse(meta.istable or meta.issingle or meta.is_submittable or meta.allow_rename)
		self.assertEqual(meta.permissions, [])
		fields = ("event_key", "sequence_key", "source_action_key", "event_digest")
		for field in fields:
			self.assertTrue(meta.get_field(field).unique)
		rows = frappe.db.sql(
			"""SELECT COLUMN_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX
			FROM information_schema.STATISTICS
			WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s""",
			("tabHRP Budget Reservation Event",),
			as_dict=True,
		)
		indexes = {}
		for row in rows:
			if not int(row.NON_UNIQUE):
				indexes.setdefault(row.INDEX_NAME, []).append((int(row.SEQ_IN_INDEX), row.COLUMN_NAME))
		unique_columns = {
			columns[0][1] for columns in indexes.values() if len(columns) == 1 and columns[0][0] == 1
		}
		self.assertTrue(set(fields).issubset(unique_columns))
