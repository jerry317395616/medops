"""Native migration and denial checks for inactive reservation event storage."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from medops.common.error_catalog import IoneApplicationError

DOCTYPE = "HRP Budget Reservation Event"
AGGREGATE = "HRP Budget Reservation"
AUTHORIZATION = "HRP Budget Reservation Authorization"
RENEWAL = "HRP Budget Reservation Renewal"


class TestBudgetReservationStorage(FrappeTestCase):
	def test_renewal_storage_denies_writes_and_has_database_unique_successor(self):
		before = frappe.db.count(RENEWAL)
		doc = frappe.get_doc(dict(doctype=RENEWAL))
		for action in (
			lambda: doc.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True),
			doc.db_insert,
			doc.db_update,
			lambda: doc.db_set("acknowledged", 1),
			doc.on_trash,
			lambda: doc.before_rename("old", "new", False),
		):
			with self.subTest(action=action), self.assertRaises(IoneApplicationError):
				action()
		self.assertEqual(frappe.db.count(RENEWAL), before)
		meta = frappe.get_meta(RENEWAL)
		self.assertFalse(meta.istable or meta.issingle or meta.is_submittable or meta.allow_rename)
		self.assertEqual(meta.permissions, [])
		for field in ("proof_hash", "session_digest"):
			self.assertTrue(meta.get_field(field).hidden and int(meta.get_field(field).permlevel) == 1)
		required = {"evidence_key", "confirmation", "one_time_key", "event_digest"}
		for field in required:
			self.assertTrue(meta.get_field(field).unique)
			self.assertTrue(frappe.db.has_column(RENEWAL, field))
		rows = frappe.db.sql(
			"""SELECT COLUMN_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX FROM information_schema.STATISTICS
			WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s""",
			("tabHRP Budget Reservation Renewal",),
			as_dict=True,
		)
		indexes = {}
		for row in rows:
			if not int(row.NON_UNIQUE):
				indexes.setdefault(row.INDEX_NAME, []).append((int(row.SEQ_IN_INDEX), row.COLUMN_NAME))
		unique_columns = {
			columns[0][1] for columns in indexes.values() if len(columns) == 1 and columns[0][0] == 1
		}
		self.assertTrue(required <= unique_columns)
		self.assertTrue(
			any(row.COLUMN_NAME == "reservation_identity" and int(row.SEQ_IN_INDEX) == 1 for row in rows)
		)

	def test_reservation_authorization_storage_is_closed_and_has_unique_keys(self):
		before = frappe.db.count(AUTHORIZATION)
		doc = frappe.get_doc(dict(doctype=AUTHORIZATION))
		for action in (
			lambda: doc.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True),
			doc.db_insert,
			doc.db_update,
			lambda: doc.db_set("acknowledged", 1),
			doc.on_trash,
			lambda: doc.before_rename("old", "new", False),
		):
			with self.subTest(action=action), self.assertRaises(IoneApplicationError):
				action()
		self.assertEqual(frappe.db.count(AUTHORIZATION), before)
		meta = frappe.get_meta(AUTHORIZATION)
		self.assertFalse(meta.istable or meta.issingle or meta.is_submittable or meta.allow_rename)
		self.assertEqual(meta.permissions, [])
		for name in ("intent_snapshot_json", "source_snapshot_json", "proof_hash", "session_digest"):
			self.assertTrue(meta.get_field(name).hidden and int(meta.get_field(name).permlevel) == 1)
		for name in ("evidence_key", "confirmation", "one_time_key", "event_digest"):
			self.assertTrue(meta.get_field(name).unique)
			self.assertTrue(frappe.db.has_column(AUTHORIZATION, name))
		rows = frappe.db.sql(
			"""SELECT COLUMN_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX FROM information_schema.STATISTICS
			WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s""",
			("tabHRP Budget Reservation Authorization",),
			as_dict=True,
		)
		indexes = {}
		for row in rows:
			if not int(row.NON_UNIQUE):
				indexes.setdefault(row.INDEX_NAME, []).append((int(row.SEQ_IN_INDEX), row.COLUMN_NAME))
		unique_columns = {
			columns[0][1] for columns in indexes.values() if len(columns) == 1 and columns[0][0] == 1
		}
		self.assertTrue({"evidence_key", "confirmation", "one_time_key", "event_digest"} <= unique_columns)

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
		for field in ("source_line", "source_allocation", "source_action", "proof_version"):
			self.assertTrue(meta.get_field(field).reqd)
			self.assertTrue(frappe.db.has_column(DOCTYPE, field))
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
		self.assertTrue(meta.get_field("source_name").search_index)
		self.assertTrue(any(row.COLUMN_NAME == "source_name" and int(row.SEQ_IN_INDEX) == 1 for row in rows))

	def test_reservation_aggregate_is_closed_and_has_anchor_columns(self):
		before = frappe.db.count(AGGREGATE)
		doc = frappe.get_doc(dict(doctype=AGGREGATE))
		for action in (
			lambda: doc.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True),
			doc.db_insert,
			doc.db_update,
			lambda: doc.db_set("event_count", 1),
			doc.before_submit,
			doc.on_trash,
			lambda: doc.before_rename("old", "new", False),
		):
			with self.subTest(action=action), self.assertRaises(IoneApplicationError):
				action()
		self.assertEqual(frappe.db.count(AGGREGATE), before)
		meta = frappe.get_meta(AGGREGATE)
		self.assertFalse(meta.istable or meta.issingle or meta.allow_rename)
		self.assertTrue(meta.is_submittable)
		self.assertEqual(meta.permissions, [])
		for field in (
			"reservation_key",
			"source_identity_key",
			"event_count",
			"last_event_digest",
			"anchor_digest",
			"campus",
			"cost_center",
			"project",
			"currency_precision",
			"subject",
		):
			self.assertTrue(frappe.db.has_column(AGGREGATE, field))
		for field in (
			"total_amount",
			"approved_amount",
			"reserved_amount",
			"committed_amount",
			"actual_amount",
			"available_amount",
		):
			self.assertEqual(meta.get_field(field).fieldtype, "Data")
		for field in ("reservation_key", "source_identity_key"):
			self.assertTrue(meta.get_field(field).unique)
		snapshot = meta.get_field("source_snapshot_json")
		self.assertTrue(
			snapshot and snapshot.fieldtype == "Long Text" and snapshot.hidden and snapshot.read_only
		)
		self.assertFalse(snapshot.reqd or snapshot.allow_on_submit)
		self.assertTrue(frappe.db.has_column(AGGREGATE, "source_snapshot_json"))
		rows = frappe.db.sql(
			"""SELECT COLUMN_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX
			FROM information_schema.STATISTICS
			WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s""",
			("tabHRP Budget Reservation",),
			as_dict=True,
		)
		indexes = {}
		for row in rows:
			if not int(row.NON_UNIQUE):
				indexes.setdefault(row.INDEX_NAME, []).append((int(row.SEQ_IN_INDEX), row.COLUMN_NAME))
		unique_columns = {
			columns[0][1] for columns in indexes.values() if len(columns) == 1 and columns[0][0] == 1
		}
		self.assertTrue({"reservation_key", "source_identity_key"}.issubset(unique_columns))
