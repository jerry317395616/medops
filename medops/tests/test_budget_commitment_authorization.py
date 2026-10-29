"""New private schema and temporary synthetic evidence, never real authorization writes."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.commitment_evidence import (
	COMMITMENT_ROW_FIELDS,
	commitment_confirmation,
	commitment_decision,
	commitment_evidence_row,
	verify_commitment_evidence_row,
)
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services.commitment_authorization import DOCTYPE, require_commitment_evidence_storage
from medops.tests.commitment_evidence_fixtures import (
	ACTOR,
	DECIDED,
	END,
	PROOF,
	SESSION,
	START,
	commitment_fixture,
	evidence_fixture,
)


class TestBudgetCommitmentAuthorization(IntegrationTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.clear_cache(doctype=DOCTYPE)
		super().tearDown()

	def test_private_schema_uniqueness_and_all_mutation_paths_remain_closed(self):
		frappe.set_user("Administrator")
		before = frappe.db.count(DOCTYPE)
		doc = frappe.get_doc(dict(doctype=DOCTYPE))
		for enabled in (False, True):
			with patch.dict(
				frappe.conf,
				{
					"medops_internal_commitment_authorization_enabled": enabled,
					"medops_internal_commitment_confirmation_enabled": enabled,
					"medops_internal_commitment_write_enabled": enabled,
				},
			):
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
					with self.subTest(enabled=enabled), self.assertRaises(IoneApplicationError):
						action()
		self.assertEqual(frappe.db.count(DOCTYPE), before)
		meta = frappe.get_meta(DOCTYPE)
		self.assertEqual(meta.permissions, [])
		self.assertFalse(meta.istable or meta.issingle or meta.is_submittable or meta.allow_rename)
		for field in COMMITMENT_ROW_FIELDS - {"name"}:
			self.assertTrue(frappe.db.has_column(DOCTYPE, field))
		for field in (
			"proof_hash",
			"session_digest",
			"intent_snapshot_json",
			"material_snapshot_json",
			"order_snapshot_json",
		):
			self.assertTrue(meta.get_field(field).hidden and int(meta.get_field(field).permlevel) == 1)
		require_commitment_evidence_storage()

	def test_private_shadow_roundtrip_successor_uniqueness_and_custom_grant_cannot_expose_rows(self):
		"""Only temporary copies receive SQL rows; real model controller is never bypassed."""
		frappe.set_user("Administrator")
		before = frappe.db.count(DOCTYPE)
		item, first, second, material, order = evidence_fixture()
		rows = [
			commitment_evidence_row(
				item, value, material_snapshot=material, order_snapshot=order, confirmed=confirmed
			)
			for value, confirmed in ((first, None), (second, first))
		]
		# Distinct template avoids MariaDB's same-name CREATE TEMPORARY ... LIKE alias failure.
		frappe.db.sql(
			"CREATE TEMPORARY TABLE `medops_commitment_auth_template` LIKE `tabHRP Budget Commitment Authorization`"
		)
		try:
			frappe.db.sql(
				"CREATE TEMPORARY TABLE `tabHRP Budget Commitment Authorization` LIKE `medops_commitment_auth_template`"
			)
			fields = tuple(sorted(COMMITMENT_ROW_FIELDS))
			columns = ", ".join(f"`{name}`" for name in fields)
			values = ", ".join(["%s"] * len(fields))

			def insert(row):
				frappe.db.sql(
					f"INSERT INTO `tabHRP Budget Commitment Authorization` ({columns}) VALUES ({values})",
					tuple(row[name] for name in fields),
				)  # nosec B608 - fixed table/field constants; bound values

			for row in rows:
				insert(row)
			stored = frappe.db.sql(
				f"SELECT {columns} FROM `tabHRP Budget Commitment Authorization` ORDER BY recorded_at",
				as_dict=True,
			)  # nosec B608 - fixed table/field constants
			self.assertEqual(verify_commitment_evidence_row(stored[0]), (item, first))
			self.assertEqual(verify_commitment_evidence_row(stored[1], first), (item, second))
			other, material, order = commitment_fixture(amount="5.00")
			other_first = commitment_confirmation(
				other,
				actor=ACTOR,
				proof_hash=first.proof_hash,
				session_digest=SESSION,
				recorded_at=START,
				expires_at=END,
			)
			other_second = commitment_decision(
				other,
				other_first,
				proof=PROOF,
				actor=ACTOR,
				session_digest=SESSION,
				recorded_at=DECIDED,
				acknowledge=True,
			)
			insert(
				commitment_evidence_row(other, other_first, material_snapshot=material, order_snapshot=order)
			)
			self.assertEqual(other_second.one_time_key, second.one_time_key)
			with self.assertRaises(Exception) as conflict:
				insert(
					commitment_evidence_row(
						other,
						other_second,
						material_snapshot=material,
						order_snapshot=order,
						confirmed=other_first,
					)
				)
			self.assertEqual(conflict.exception.args[0], 1062)
			self.assertIn("one_time_key", str(conflict.exception))
			# A test-only Custom DocPerm grant still cannot bypass the private document hooks.
			frappe.get_doc(
				dict(
					doctype="Custom DocPerm",
					parent=DOCTYPE,
					parenttype="DocType",
					parentfield="permissions",
					role="HRP Budget Manager",
					permlevel=0,
					read=1,
					write=1,
					create=1,
				)
			).insert(ignore_permissions=True)
			user = "commitment-private-" + uuid4().hex[:12] + "@example.com"
			frappe.get_doc(
				dict(
					doctype="User",
					email=user,
					first_name="Synthetic private evidence reader",
					enabled=1,
					user_type="System User",
					send_welcome_email=0,
					roles=[dict(role="HRP Budget Manager")],
				)
			).insert(ignore_permissions=True)
			frappe.clear_cache(doctype=DOCTYPE)
			self.assertTrue(
				any(
					row.role == "HRP Budget Manager" and row.read
					for row in frappe.get_meta(DOCTYPE, cached=False).permissions
				)
			)
			frappe.set_user(user)
			try:
				visible = frappe.get_list(DOCTYPE, fields=["name"])
			except frappe.PermissionError:
				visible = []
			self.assertEqual(visible, [])
			private = frappe.get_doc(DOCTYPE, first.name)
			with self.assertRaises(frappe.PermissionError):
				private.check_permission("read")
		finally:
			frappe.set_user("Administrator")
			frappe.db.rollback()
			frappe.db.sql("DROP TEMPORARY TABLE IF EXISTS `tabHRP Budget Commitment Authorization`")
			frappe.db.sql("DROP TEMPORARY TABLE IF EXISTS `medops_commitment_auth_template`")
			frappe.clear_cache(doctype=DOCTYPE)
		self.assertEqual(frappe.db.count(DOCTYPE), before)
