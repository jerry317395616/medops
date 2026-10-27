from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions
from frappe.custom.doctype.property_setter.property_setter import make_property_setter
from frappe.tests.test_api import FrappeAPITestCase

from medops.api.v1.finance import ledger as ledger_api
from medops.common.budget_ledger import LEDGER, LEDGER_PUBLIC_FIELDS, RELEASE
from medops.common.budget_planning import VALUE
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.api.statement_reads import statement_connection
from medops.hrp_budget.services import budget_statement as statements
from medops.services.module_registry import load_module_registry
from medops.tests import test_budget_consolidation as source_fixture
from medops.tests import test_budget_release_execution as execution_fixture
from medops.tests.scope_fixtures import share_test_records
from medops.tests.test_budget_release_preparation import release_rule_fixtures

ACTOR = source_fixture.MAKERS[0]
PREFIX = source_fixture.PREFIX + "ledger."


class TestBudgetStatement(FrappeAPITestCase):
	login_actor = execution_fixture.TestBudgetReleaseExecution.login_actor
	success = execution_fixture.TestBudgetReleaseExecution.success
	post_command = execution_fixture.TestBudgetReleaseExecution.post_command
	request_approval = execution_fixture.TestBudgetReleaseExecution.request_approval
	decide = execution_fixture.TestBudgetReleaseExecution.decide
	new_sources = execution_fixture.TestBudgetReleaseExecution.new_sources
	proposal = execution_fixture.TestBudgetReleaseExecution.proposal
	seal = execution_fixture.TestBudgetReleaseExecution.seal
	new_release = execution_fixture.TestBudgetReleaseExecution.new_release
	preview = execution_fixture.TestBudgetReleaseExecution.preview
	confirm = execution_fixture.TestBudgetReleaseExecution.confirm
	authorize = execution_fixture.TestBudgetReleaseExecution.authorize
	run_job = execution_fixture.TestBudgetReleaseExecution.run_job
	claim = execution_fixture.TestBudgetReleaseExecution.claim
	execute = execution_fixture.TestBudgetReleaseExecution.execute

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.org, cls.units, cls.value, cls.fund = source_fixture.fixtures()
		release_rule_fixtures()

	def setUp(self):
		super().setUp()
		self.new_release()
		self.authorize()
		self.assertEqual(self.execute(self.claim())["status"], "Posted")
		self.rows = {
			row.organization_unit: row.name
			for row in frappe.get_all(
				LEDGER, filters={"voucher_no": self.release}, fields=["name", "organization_unit"]
			)
		}
		self.permission_names = []
		self.add_user_permission(self.units["A"])
		frappe.share.remove("HRP Organization Unit", self.units["B"], ACTOR)
		frappe.local.db.commit()
		primary = frappe.local.db
		try:
			with self.secondary_connection():
				self._secondary_connection.rollback()
		finally:
			frappe.local.db = primary
		self.login_actor(ACTOR)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for name in self.permission_names:
			if frappe.db.exists("User Permission", name):
				frappe.delete_doc("User Permission", name, ignore_permissions=True)
		share_test_records("HRP Organization Unit", (self.units["B"],), (ACTOR,))
		frappe.local.db.commit()
		frappe.cache.hdel("user_permissions", ACTOR)
		super().tearDown()

	def add_user_permission(self, unit):
		self.permission_names.append(
			frappe.get_doc(
				dict(
					doctype="User Permission",
					user=ACTOR,
					allow="HRP Organization Unit",
					for_value=unit,
					apply_to_all_doctypes=1,
					hide_descendants=1,
				)
			)
			.insert(ignore_permissions=True)
			.name
		)

	def query(self, **values):
		return (
			dict(organization_unit=self.units["A"], fiscal_year=source_fixture.YEAR, currency="CNY") | values
		)

	def statement(self, payload=None, **values):
		return self.get(
			self.method(PREFIX + "statement"), dict(payload=frappe.as_json(payload or self.query()), **values)
		)

	def entry(self, name):
		return self.get(self.method(PREFIX + "get_entry"), {"name": name})

	def denied(self, response, codes=("IONE-CORE-0002", "IONE-CORE-0005")):
		data = json.dumps(response.get_json())
		self.assertIn(response.status_code, (403, 409, 500))
		self.assertTrue(any(code in data for code in codes), data)
		self.assertNotIn('"rows"', data)
		self.assertNotIn('"totals"', data)

	def test_complete_original_unit_projection_hides_other_leaf_and_private_batch_fields(self):
		book = self.success(self.statement())
		self.assertGreaterEqual(book["total_rows"], 1)
		self.assertTrue(all(row["organization_unit"] == self.units["A"] for row in book["rows"]))
		self.assertEqual(book["totals"]["precision"], 9)
		self.assertNotIn(self.release, json.dumps(book))
		self.assertNotIn(self.units["B"], json.dumps(book))
		item = self.success(self.entry(self.rows[self.units["A"]]))
		self.assertEqual(set(item), set(LEDGER_PUBLIC_FIELDS))
		self.assertEqual(item["amount"], "99999.999999999")
		self.denied(self.entry(self.rows[self.units["B"]]))
		self.denied(self.statement(self.query(organization_unit=self.units["B"])))

	def test_current_page_labels_are_authorized_and_leave_the_financial_digest_unchanged(self):
		book = self.success(self.statement(limit=1))
		self.assertEqual(len(book["rows"]), 1)
		self.assertEqual(len(book["display_labels"]), 1)
		row = book["rows"][0]
		self.assertEqual(row["budget_subject"], self.value)
		self.assertEqual(row["funding_source"], self.fund)
		self.assertEqual(
			book["display_labels"][0], {"budget_subject": "设备预算", "funding_source": "自有资金"}
		)
		empty = self.success(self.statement(self.query(to_date="2026-01-01")))
		self.assertEqual(empty["rows"], [])
		self.assertNotIn("display_labels", empty)
		page = self.success(self.statement(offset=1, limit=1, expected_digest=book["statement_digest"]))
		self.assertEqual(page["statement_digest"], book["statement_digest"])
		self.assertEqual(len(page["display_labels"]), len(page["rows"]))

	def test_display_name_revocation_after_label_collection_never_returns_the_book(self):
		original = statements.statement_display_labels
		setters = []
		fired = False

		def revoke_after_labels(*args, **kwargs):
			nonlocal fired
			labels = original(*args, **kwargs)
			if not fired and labels:
				fired = True
				primary = frappe.local.db
				with self.secondary_connection():
					frappe.set_user("Administrator")
					self.assertFalse(
						frappe.db.exists(
							"Property Setter",
							{"doc_type": VALUE, "field_name": "display_name", "property": "permlevel"},
						)
					)
					setters.append(
						make_property_setter(
							VALUE, "display_name", "permlevel", 2, "Int", validate_fields_for_doctype=False
						).name
					)
					self._secondary_connection.commit()
				self.assertIs(frappe.local.db, primary)
				frappe.set_user(ACTOR)
			return labels

		try:
			with patch.object(statements, "statement_display_labels", side_effect=revoke_after_labels):
				response = self.statement(limit=1)
				self.denied(response)
				self.assertNotIn("display_labels", json.dumps(response.get_json()))
			self.assertTrue(fired)
		finally:
			with source_fixture.committed_test_changes(self):
				for name in setters:
					frappe.delete_doc("Property Setter", name, ignore_permissions=True)
					self.assertFalse(frappe.db.exists("Property Setter", name))
				frappe.clear_cache(doctype=VALUE)

	def test_empty_date_scope_is_an_explicit_complete_zero_book(self):
		book = self.success(self.statement(self.query(to_date="2026-01-01")))
		self.assertEqual((book["total_rows"], book["rows"], book["totals"]["closing"]), (0, [], "0"))
		self.assertEqual(book["context"]["currency"], "CNY")
		self.assertEqual(book["context"]["from_date"], "2026-01-01")

	def test_opening_and_complete_reversal_use_exact_original_unit_amounts(self):
		before = self.success(self.statement(self.query(from_date="2026-10-27", to_date="2026-10-27")))
		original = self.release
		self.login_actor(source_fixture.MANAGER)
		self.release = self.success(
			self.post_command(
				"release.create_reversal",
				dict(
					release=original,
					expected_revision=3,
					posting_date="2026-10-27",
					reason="原始单位完整账簿反向验收",
				),
			)
		)["name"]
		self.authorize()
		self.assertEqual(self.execute(self.claim())["status"], "Posted")
		self.login_actor(ACTOR)
		book = self.success(self.statement(self.query(from_date="2026-10-27", to_date="2026-10-27")))
		self.assertEqual(book["totals"]["opening"], before["totals"]["opening"])
		self.assertEqual(
			Decimal(book["totals"]["credit"]),
			Decimal("99999.999999999") + Decimal(before["totals"]["credit"]),
		)
		self.assertEqual(
			Decimal(book["totals"]["closing"]),
			Decimal(before["totals"]["closing"]) - Decimal("99999.999999999"),
		)
		self.assertEqual(book["rows"][-1]["organization_unit"], self.units["A"])

	def test_pagination_digest_rejects_a_changed_complete_book(self):
		first = self.success(self.statement(limit=1))
		next_page = self.success(self.statement(offset=1, limit=1, expected_digest=first["statement_digest"]))
		self.assertEqual(next_page["statement_digest"], first["statement_digest"])
		self.new_release()
		self.authorize()
		self.assertEqual(self.execute(self.claim())["status"], "Posted")
		self.login_actor(ACTOR)
		self.denied(self.statement(expected_digest=first["statement_digest"]), ("IONE-CORE-0005",))

	def test_parent_cursor_and_changed_final_batch_set_fail_closed(self):
		original_get_all = frappe.get_all
		queries = []

		def track_parent_queries(doctype, *args, **kwargs):
			filters = kwargs.get("filters") or {}
			result = original_get_all(doctype, *args, **kwargs)
			if doctype == RELEASE and filters.get("status") == "Posted" and kwargs.get("pluck") == "name":
				queries.append((dict(filters), result))
			return result

		with (
			patch.object(statements, "STATEMENT_PARENT_PAGE_SIZE", 1),
			patch.object(frappe, "get_all", side_effect=track_parent_queries),
		):
			book = self.success(self.statement(limit=1))
		self.assertGreaterEqual(book["total_rows"], 1)
		self.assertGreaterEqual(len(queries), 2)
		self.assertEqual(queries[1][0]["name"], [">", queries[0][1][-1]])

		original_batches = statements.statement_batches
		calls = []

		def changed_final_set(context, *, export=False):
			names = original_batches(context, export=export)
			calls.append(names)
			return (*names, "synthetic-concurrent-parent") if len(calls) == 2 else names

		with patch.object(statements, "statement_batches", side_effect=changed_final_set):
			self.denied(self.statement(), ("IONE-CORE-0005",))
		self.assertEqual(len(calls), 2)

	def test_missing_or_tampered_query_rows_cannot_become_partial_totals(self):
		original = frappe.get_all
		for mode in ("missing", "amount"):

			def corrupted(doctype, *args, _mode=mode, **kwargs):
				rows = original(doctype, *args, **kwargs)
				if (
					doctype == LEDGER
					and kwargs.get("fields") == ["*"]
					and (kwargs.get("filters") or {}).get("voucher_no") == self.release
				):
					rows = [frappe._dict(dict(row)) for row in rows]
					if _mode == "missing":
						rows = [row for row in rows if row.organization_unit != self.units["A"]]
					else:
						for row in rows:
							if row.organization_unit == self.units["A"]:
								row.amount = "2.000000000"
				return rows

			with self.subTest(mode=mode), patch("frappe.get_all", side_effect=corrupted):
				self.denied(self.statement(), ("IONE-CORE-0009",))

	@contextmanager
	def revoke_after_entry(self, revoke, *, final_only=False):
		original = statements.authorize_entry
		refresh = statements.refresh_statement_actor
		fired = False
		final = False

		def final_refresh():
			nonlocal final
			final = True
			return refresh()

		def authorize(*args, **kwargs):
			nonlocal fired
			result = original(*args, **kwargs)
			if not fired and (not final_only or final):
				fired = True
				primary = frappe.local.db
				with self.secondary_connection():
					frappe.set_user("Administrator")
					try:
						revoke()
						self._secondary_connection.commit()
					except Exception:
						self._secondary_connection.rollback()
						raise
				self.assertIs(frappe.local.db, primary)
				frappe.set_user(ACTOR)
			return result

		with (
			patch.object(statements, "authorize_entry", side_effect=authorize),
			patch.object(statements, "refresh_statement_actor", side_effect=final_refresh),
		):
			yield
		self.assertTrue(fired)

	def test_committed_source_share_revocation_mid_read_denies_the_whole_book(self):
		try:
			with self.revoke_after_entry(
				lambda: frappe.share.remove("HRP Hospital", source_fixture.HOSPITAL, ACTOR)
			):
				self.denied(self.statement())
		finally:
			with source_fixture.committed_test_changes(self):
				share_test_records("HRP Hospital", (source_fixture.HOSPITAL,), (ACTOR,))

	def test_final_phase_revocation_is_not_hidden_by_access_graph_success_cache(self):
		try:
			with self.revoke_after_entry(
				lambda: frappe.share.remove("HRP Hospital", source_fixture.HOSPITAL, ACTOR), final_only=True
			):
				response = self.statement()
				self.assertEqual(response.status_code, 409)
				self.denied(response, ("IONE-CORE-0005",))
		finally:
			with source_fixture.committed_test_changes(self):
				share_test_records("HRP Hospital", (source_fixture.HOSPITAL,), (ACTOR,))

	def test_current_ledger_field_revocation_denies_even_when_rows_were_already_validated(self):
		setters = []

		def revoke():
			setters.append(
				make_property_setter(
					LEDGER, "amount", "permlevel", 2, "Int", validate_fields_for_doctype=False
				).name
			)

		try:
			with self.revoke_after_entry(revoke):
				self.denied(self.statement())
		finally:
			with source_fixture.committed_test_changes(self):
				for name in setters:
					frappe.delete_doc("Property Setter", name, ignore_permissions=True)
					self.assertFalse(frappe.db.exists("Property Setter", name))
				frappe.clear_cache(doctype=LEDGER)

	def test_native_user_permission_revocation_wins_over_poisoned_redis(self):
		frappe.set_user(ACTOR)
		before = deepcopy(get_user_permissions(ACTOR))

		def revoke():
			frappe.delete_doc("User Permission", self.permission_names[0], ignore_permissions=True)
			self.add_user_permission(self.units["B"])
			frappe.db.after_commit.add(lambda: frappe.cache.hset("user_permissions", ACTOR, before))

		with self.revoke_after_entry(revoke):
			self.denied(self.statement())

	def test_export_is_complete_exact_text_and_formula_like_description_stays_literal(self):
		build = source_fixture.build_plan

		def described(payload):
			return build(
				dict(
					payload, lines=[dict(row, description="=1+1 <b>literal</b>") for row in payload["lines"]]
				)
			)

		with patch.object(source_fixture, "build_plan", side_effect=described):
			self.new_release()
		self.authorize()
		self.assertEqual(self.execute(self.claim())["status"], "Posted")
		# Use an existing principal with every native master export grant for
		# positive file verification. Installation never widens those grants;
		# department read and export-denial cases use the restricted actor.
		self.login_actor("Administrator")
		book = self.success(self.statement())
		response = self.get(
			self.method(PREFIX + "export_statement"),
			dict(payload=frappe.as_json(self.query()), expected_digest=book["statement_digest"]),
		)
		self.assertEqual(response.status_code, 200)
		self.assertIn(".xlsx", response.headers.get("Content-Disposition", ""))
		with ZipFile(BytesIO(response.data)) as archive:
			# Only the workbook just generated by our authenticated API, no uploaded XML.
			xml = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))  # nosec B314
		ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
		self.assertEqual(len(xml.findall("s:sheetData/s:row", ns)), book["total_rows"] + 1)
		self.assertFalse(xml.findall(".//s:f", ns))
		texts = [node.text or "" for node in xml.findall(".//s:t", ns)]
		self.assertIn("=1+1 <b>literal</b>", texts)
		self.assertIn("99999.999999999", texts)
		self.assertNotIn(self.release, texts)
		self.assertNotIn(self.units["B"], texts)
		self.assertTrue(all(node.get("t") == "inlineStr" for node in xml.findall(".//s:c", ns)))

	def test_export_requires_current_scope_export_grant(self):
		book = self.success(self.statement())
		frappe.set_user(ACTOR)
		with statement_connection():
			self.assertEqual(statements.statement_actor(), ACTOR)
			statements.require_statement_scope(book["context"], export=True)
		frappe.set_user("Administrator")
		name = "COD032C-scope-" + ACTOR.split("@")[0]
		scope = frappe.get_doc("HRP Access Scope", name)
		original = [int(row.allow_export) for row in scope.members]
		try:
			for row in scope.members:
				row.allow_export = 0
			scope.save(ignore_permissions=True)
			frappe.local.db.commit()
			self.login_actor(ACTOR)
			self.success(self.statement())
			# Isolate the scope guard from independent native master export
			# requirements; the API must reject too, without partial results.
			frappe.set_user(ACTOR)
			with statement_connection():
				self.assertEqual(statements.statement_actor(), ACTOR)
				with self.assertRaises(IoneApplicationError) as failure:
					statements.require_statement_scope(book["context"], export=True)
				self.assertEqual(failure.exception.code, "IONE-CORE-0002")
			self.denied(
				self.get(
					self.method(PREFIX + "export_statement"),
					dict(payload=frappe.as_json(self.query()), expected_digest=book["statement_digest"]),
				)
			)
		finally:
			frappe.set_user("Administrator")
			scope = frappe.get_doc("HRP Access Scope", name)
			for row, value in zip(scope.members, original, strict=True):
				row.allow_export = value
			scope.save(ignore_permissions=True)
			frappe.local.db.commit()

	def test_owned_read_preserves_primary_writes_session_callbacks_and_enforces_database_read_only(self):
		frappe.set_user("Administrator")
		module = load_module_registry().modules[0].module
		old = frappe.db.get_value("HRP Module Setting", module, "workspace_route")
		frappe.db.set_value("HRP Module Setting", module, "workspace_route", "COD033-primary-uncommitted")
		frappe.set_user(ACTOR)
		primary = frappe.local.db
		session = frappe.local.session
		sid = session.sid
		callbacks = []
		callback_manager = primary.after_commit
		primary.after_commit.add(lambda: callbacks.append("caller committed"))
		observed = []
		original = statements.ReadBudgetStatement.perform

		def perform(service, command):
			observed.append(
				(
					frappe.local.db is primary,
					frappe.db.sql("SELECT @@tx_read_only")[0][0],
					frappe.db.get_value("HRP Module Setting", module, "workspace_route"),
				)
			)
			return original(service, command)

		try:
			with patch.object(statements.ReadBudgetStatement, "perform", new=perform):
				ledger_api.statement(self.query())
			self.assertEqual(observed, [(False, 1, old)])
			self.assertIs(frappe.local.db, primary)
			self.assertIs(frappe.local.session, session)
			self.assertEqual(frappe.local.session.sid, sid)
			self.assertIs(primary.after_commit, callback_manager)
			self.assertEqual(callbacks, [])
			self.assertEqual(
				frappe.db.get_value("HRP Module Setting", module, "workspace_route"),
				"COD033-primary-uncommitted",
			)
			with statement_connection():
				with self.assertRaises(frappe.InReadOnlyMode):
					frappe.db.set_value(
						"HRP Module Setting", module, "workspace_route", "COD033-read-write-forbidden"
					)
		finally:
			primary.rollback()
		self.assertEqual(callbacks, [])
		self.assertEqual(frappe.db.get_value("HRP Module Setting", module, "workspace_route"), old)
