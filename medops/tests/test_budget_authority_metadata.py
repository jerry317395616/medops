"""Actual native metadata/permission freshness; no financial posting fixtures."""

from __future__ import annotations

import json
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.model import get_permitted_fields
from frappe.tests.utils import FrappeTestCase

from medops.hrp_budget.services import authority_metadata as metadata
from medops.hrp_budget.services.release_execution import AUTHORITY_ROOTS, refresh_financial_authority

TARGET = "HRP Budget Plan"


class TestBudgetAuthorityMetadata(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		self.assertTrue(
			metadata.supported_native_constructor(), "Pinned profile must exercise bulk construction"
		)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.clear_cache()
		super().tearDown()

	def plain(self, metas):
		return {name: json.loads(frappe.as_json(meta.as_dict())) for name, meta in metas.items()}

	def native(self, names):
		return {name: frappe.get_meta(name, cached=False) for name in names}

	def user(self):
		user = "cod035-metadata-" + uuid4().hex[:12] + "@example.com"
		frappe.get_doc(
			dict(
				doctype="User",
				email=user,
				first_name="Synthetic metadata actor",
				enabled=1,
				user_type="System User",
				send_welcome_email=0,
				roles=[dict(role="HRP Budget Manager")],
			)
		).insert(ignore_permissions=True)
		return user

	def test_complete_native_graph_is_identical_and_covers_real_child_metadata(self):
		self.native(AUTHORITY_ROOTS)
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as native_sql:
			expected = self.plain(self.native(AUTHORITY_ROOTS))
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as bulk_sql:
			actual = self.plain(metadata.fresh_metadata_batch(AUTHORITY_ROOTS))
		self.assertEqual(actual, expected)
		self.assertLess(bulk_sql.call_count, native_sql.call_count)
		pending, seen = set(AUTHORITY_ROOTS), set()
		while pending:
			batch = metadata.fresh_metadata_batch(pending)
			self.assertEqual(self.plain(batch), self.plain(self.native(pending)))
			seen.update(pending)
			pending = {
				field.options
				for meta in batch.values()
				for field in meta.fields
				if field.fieldtype in {"Table", "Table MultiSelect"}
				and field.options
				and field.options not in seen
			}
		self.assertGreater(len(seen), len(AUTHORITY_ROOTS))

	def test_budget_actor_without_doctype_admin_rights_gets_identical_metadata(self):
		actor = self.user()
		frappe.set_user(actor)
		refresh_financial_authority(actor)
		self.assertFalse(frappe.has_permission("DocType", "read"))
		names = {TARGET, "HRP Budget Release Entry", "Workflow", "Account"}
		self.assertEqual(self.plain(metadata.fresh_metadata_batch(names)), self.plain(self.native(names)))

	def test_uncommitted_field_permission_change_and_rollback_are_visible(self):
		actor = self.user()
		frappe.set_user(actor)
		refresh_financial_authority(actor)
		self.assertIn("total_amount", get_permitted_fields(TARGET, user=actor))
		frappe.db.savepoint("metadata_before_field_change")
		name = frappe.db.get_value("DocField", {"parent": TARGET, "fieldname": "total_amount"}, "name")
		self.assertTrue(name)
		frappe.db.set_value("DocField", name, "permlevel", 2, update_modified=False)
		refresh_financial_authority(actor)
		self.assertEqual(frappe.get_meta(TARGET).get_field("total_amount").permlevel, 2)
		self.assertNotIn("total_amount", get_permitted_fields(TARGET, user=actor))
		self.assertEqual(
			self.plain(metadata.fresh_metadata_batch({TARGET})), self.plain(self.native({TARGET}))
		)
		frappe.db.rollback(save_point="metadata_before_field_change")
		refresh_financial_authority(actor)
		self.assertIn("total_amount", get_permitted_fields(TARGET, user=actor))

	def test_uncommitted_docperm_revocation_wins_over_warm_metadata(self):
		from frappe.permissions import setup_custom_perms

		actor = self.user()
		for table in ("DocPerm", "Custom DocPerm"):
			with self.subTest(table=table):
				if table == "Custom DocPerm":
					frappe.set_user("Administrator")
					setup_custom_perms(TARGET)
				frappe.set_user(actor)
				refresh_financial_authority(actor)
				self.assertTrue(frappe.has_permission(TARGET, "read"))
				name = frappe.db.get_value(
					table, {"parent": TARGET, "role": "HRP Budget Manager", "permlevel": 0}, "name"
				)
				self.assertTrue(name)
				frappe.db.savepoint("metadata_before_role_change")
				frappe.db.set_value(table, name, "read", 0, update_modified=False)
				refresh_financial_authority(actor)
				self.assertFalse(frappe.has_permission(TARGET, "read"))
				frappe.db.rollback(save_point="metadata_before_role_change")
				refresh_financial_authority(actor)
				self.assertTrue(frappe.has_permission(TARGET, "read"))

	def test_current_role_assignment_is_reread_without_metadata_permission_success_cache(self):
		actor = self.user()
		frappe.set_user(actor)
		refresh_financial_authority(actor)
		self.assertIn("HRP Budget Manager", frappe.get_roles())
		frappe.db.savepoint("metadata_before_assignment_change")
		frappe.db.delete("Has Role", {"parent": actor, "parenttype": "User", "role": "HRP Budget Manager"})
		refresh_financial_authority(actor)
		self.assertNotIn("HRP Budget Manager", frappe.get_roles())
		self.assertFalse(frappe.has_permission(TARGET, "read"))
		frappe.db.rollback(save_point="metadata_before_assignment_change")
		refresh_financial_authority(actor)
		self.assertIn("HRP Budget Manager", frappe.get_roles())

	def test_live_native_reader_override_is_respected_by_fresh_fallback(self):
		names = {TARGET, "Workflow"}
		expected = self.plain(self.native(names))
		with patch.object(frappe, "get_meta", wraps=frappe.get_meta) as reader:
			self.assertFalse(metadata.supported_native_constructor())
			self.assertEqual(self.plain(metadata.fresh_metadata_batch(names)), expected)
			fresh = [call.args[0] for call in reader.call_args_list if call.kwargs.get("cached") is False]
			self.assertEqual(fresh, sorted(names))

	def test_source_compatibility_mismatch_uses_native_fresh_reads(self):
		names = {TARGET, "Workflow"}
		expected = self.plain(self.native(names))
		with patch.object(metadata, "CONSTRUCTOR_HASHES", ("0" * 64,) * len(metadata.CONSTRUCTOR_HASHES)):
			self.assertFalse(metadata.supported_native_constructor())
			self.assertEqual(self.plain(metadata.fresh_metadata_batch(names)), expected)
		self.assertTrue(metadata.supported_native_constructor())

	def test_bounds_invalid_names_and_missing_native_metadata_fail_without_partial_success(self):
		for names in ([], "Company", [" Company"], [None], ["Unknown " + str(index) for index in range(257)]):
			with self.subTest(names_type=type(names).__name__), self.assertRaises(ValueError):
				metadata.fresh_metadata_batch(names)
		with self.assertRaises(frappe.DoesNotExistError):
			metadata.fresh_metadata_batch({"Unknown metadata " + uuid4().hex})
