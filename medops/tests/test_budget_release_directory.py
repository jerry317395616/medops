from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from hmac import compare_digest
from typing import Any
from unittest.mock import patch

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions
from frappe.custom.doctype.property_setter.property_setter import make_property_setter
from frappe.tests.test_api import FrappeAPITestCase

from medops.api.v1.finance import release as release_api
from medops.common.budget_ledger import LEDGER, RELEASE, RELEASE_PUBLIC_FIELDS
from medops.common.budget_release_directory import SOURCE_PUBLIC_FIELDS
from medops.common.budget_workflow import CASE
from medops.hrp_budget.api.statement_reads import statement_connection
from medops.hrp_budget.services import release_directory as directory
from medops.hrp_budget.services.access_graph import access_graph
from medops.hrp_budget.services.approval_access import case_permission
from medops.hrp_budget.services.statement_authority import refresh_statement_actor
from medops.hrp_budget.services.submission_access import submission_permission
from medops.hrp_workflow_authorization.services.access_scope import grants_for
from medops.services.errors import raise_ione_error
from medops.services.module_registry import load_module_registry
from medops.tests import test_budget_consolidation as source_fixture
from medops.tests import test_budget_release_confirmation as confirmation_fixture
from medops.tests.scope_fixtures import share_test_records
from medops.tests.test_budget_release_preparation import release_rule_fixtures

ACTOR = source_fixture.MANAGER
PREFIX = source_fixture.PREFIX + "release."


class TestBudgetReleaseDirectory(FrappeAPITestCase):
	login_actor = confirmation_fixture.TestBudgetReleaseConfirmation.login_actor
	success = confirmation_fixture.TestBudgetReleaseConfirmation.success
	post_command = confirmation_fixture.TestBudgetReleaseConfirmation.post_command
	request_approval = confirmation_fixture.TestBudgetReleaseConfirmation.request_approval
	decide = confirmation_fixture.TestBudgetReleaseConfirmation.decide
	new_sources = confirmation_fixture.TestBudgetReleaseConfirmation.new_sources
	proposal = confirmation_fixture.TestBudgetReleaseConfirmation.proposal
	seal = confirmation_fixture.TestBudgetReleaseConfirmation.seal
	new_release = confirmation_fixture.TestBudgetReleaseConfirmation.new_release
	preview = confirmation_fixture.TestBudgetReleaseConfirmation.preview
	confirm = confirmation_fixture.TestBudgetReleaseConfirmation.confirm

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.org, cls.units, cls.value, cls.fund = source_fixture.fixtures()
		release_rule_fixtures()

	def setUp(self):
		super().setUp()
		self.new_release()
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
		super().tearDown()

	def filters(self, **values):
		return (
			dict(
				company=source_fixture.COMPANY,
				hospital=source_fixture.HOSPITAL,
				fiscal_year=source_fixture.YEAR,
			)
			| values
		)

	def record(self):
		return self.get(self.method(PREFIX + "get_record"), {"name": self.release})

	def listing(self, filters=None, endpoint="list_records", **values):
		return self.get(
			self.method(PREFIX + endpoint),
			dict(filters=frappe.as_json(filters or self.filters(name=self.release)), **values),
		)

	def denied(self, response, code="IONE-CORE-0002", statuses=(403, 409)):
		self.assertIn(response.status_code, statuses)
		data = response.get_json()
		self.assertEqual(data.get("ione_error", {}).get("code"), code)
		self.assertNotIsInstance(data.get("message"), dict)

	def test_owned_record_and_directory_return_only_reviewed_complete_source_headers(self):
		doc = self.success(self.record())
		self.assertEqual(set(doc), set(RELEASE_PUBLIC_FIELDS))
		self.assertEqual(doc["total_amount"], "100000.000000001")
		self.assertEqual(doc["status"], "Prepared")
		items = self.success(self.listing())["items"]
		self.assertEqual(items, [doc])
		self.assertNotIn("outbox_message", doc)
		self.assertNotIn("intent_digest", doc)

	def test_read_graph_cache_binds_supplied_case_fields_and_explicit_actor(self):
		frappe.set_user(ACTOR)
		with statement_connection(), access_graph():
			case = frappe.get_doc(CASE, frappe.db.get_value(RELEASE, self.release, "source_approval"))
			self.assertTrue(case_permission(case, user=ACTOR, ptype="read"))
			tampered = frappe.get_doc(case.as_dict())
			tampered.total_amount = "1.000000000"
			self.assertFalse(case_permission(tampered, user=ACTOR, ptype="read"))
			self.assertFalse(case_permission(case, user="Guest", ptype="read"))
			self.assertTrue(case_permission(case, user=ACTOR, ptype="read"))
			source = frappe.get_doc("HRP Budget Submission", case.submission)
			self.assertTrue(submission_permission(source, user=ACTOR, ptype="read"))
			changed_source = frappe.get_doc(source.as_dict())
			changed_source.total_amount = "1.000000000"
			self.assertFalse(submission_permission(changed_source, user=ACTOR, ptype="read"))
			self.assertFalse(submission_permission(source, user="Guest", ptype="read"))
			self.assertTrue(submission_permission(source, user=ACTOR, ptype="read"))

	def test_directory_digest_binds_pagination_and_changes_after_authorization(self):
		first = self.success(self.listing(limit=1))
		self.assertEqual(
			self.success(self.listing(offset=1, limit=1, expected_digest=first["list_digest"]))["items"], []
		)
		self.success(self.confirm(self.success(self.preview())))
		self.denied(self.listing(expected_digest=first["list_digest"]), "IONE-CORE-0005")
		current = self.success(self.listing())["items"][0]
		self.assertEqual((current["status"], current["revision"]), ("Authorized", 2))

	def test_explicit_authority_refresh_invalidates_success_in_an_outer_graph(self):
		frappe.set_user(ACTOR)
		try:
			with statement_connection(), access_graph():
				case = frappe.get_doc(CASE, frappe.db.get_value(RELEASE, self.release, "source_approval"))
				self.assertTrue(case_permission(case, user=ACTOR, ptype="read"))
				with source_fixture.committed_test_changes(self):
					frappe.share.remove("HRP Hospital", source_fixture.HOSPITAL, ACTOR)
				frappe.set_user(ACTOR)
				refresh_statement_actor()
				self.assertFalse(case_permission(case, user=ACTOR, ptype="read"))
		finally:
			with source_fixture.committed_test_changes(self):
				share_test_records("HRP Hospital", (source_fixture.HOSPITAL,), (ACTOR,))

	def test_scope_grants_reuse_only_the_current_owned_phase_and_refresh_after_revocation(self):
		frappe.set_user(ACTOR)
		scope_name = "COD032C-scope-" + ACTOR.split("@")[0]
		primary = frappe.local.db
		queries = []
		database_class = type(primary)
		original = database_class.sql

		def sql(connection, query, *args, **kwargs):
			if "FROM `tabHRP Access Scope` AS scope" in str(query):
				queries.append(connection)
			return original(connection, query, *args, **kwargs)

		try:
			with patch.object(database_class, "sql", new=sql):
				with access_graph():
					self.assertTrue(grants_for(ACTOR, CASE, "read"))
					self.assertTrue(grants_for(ACTOR, CASE, "read"))
				self.assertEqual(len(queries), 2)
				with statement_connection():
					with access_graph():
						first = grants_for(ACTOR, CASE, "read")
						self.assertTrue(first)
						self.assertIs(grants_for(ACTOR, CASE, "read"), first)
						self.assertEqual(len(queries), 3)
						# Another permission action and another date cannot reuse it.
						self.assertTrue(grants_for(ACTOR, CASE, "export"))
						with patch(
							"medops.hrp_workflow_authorization.services.access_scope.nowdate",
							return_value="2026-10-28",
						):
							self.assertTrue(grants_for(ACTOR, CASE, "read"))
						self.assertEqual(len(queries), 5)
						with source_fixture.committed_test_changes(self):
							frappe.get_doc("HRP Access Scope", scope_name).update({"enabled": 0}).save(
								ignore_permissions=True
							)
						frappe.set_user(ACTOR)
						refresh_statement_actor()
						self.assertEqual(grants_for(ACTOR, CASE, "read"), ())
						self.assertEqual(len(queries), 6)
					with access_graph():
						self.assertEqual(grants_for(ACTOR, CASE, "read"), ())
						self.assertEqual(len(queries), 7)
		finally:
			with source_fixture.committed_test_changes(self):
				frappe.get_doc("HRP Access Scope", scope_name).update({"enabled": 1}).save(
					ignore_permissions=True
				)

	def test_eligible_sources_are_approved_consolidated_and_not_already_prepared(self):
		case = frappe.db.get_value(RELEASE, self.release, "source_approval")
		self.assertEqual(
			self.success(self.listing(self.filters(name=case), endpoint="eligible_sources"))["items"], []
		)
		self.sources = self.new_sources()
		name = self.success(self.seal())["name"]
		case = self.request_approval(name)
		self.login_actor(source_fixture.REVIEWER)
		self.success(self.decide(case))
		self.login_actor(ACTOR)
		item = self.success(self.listing(self.filters(name=case), endpoint="eligible_sources"))["items"][0]
		self.assertEqual(set(item), set(SOURCE_PUBLIC_FIELDS))
		self.assertEqual((item["revision"], item["line_count"], item["submission"]), (2, 2, name))
		self.success(
			self.post_command(
				"release.create",
				dict(
					approval_case=case,
					expected_approval_revision=item["revision"],
					posting_date="2026-10-26",
					reason="目录来源验收",
				),
			)
		)
		self.assertEqual(
			self.success(self.listing(self.filters(name=case), endpoint="eligible_sources"))["items"], []
		)

	def test_scope_filters_bounds_and_ordinary_department_role_do_not_expose_parent_records(self):
		self.assertEqual(
			self.success(self.listing(self.filters(organization_unit=self.units["A"])))["items"], []
		)
		invalid_pages: list[dict[str, Any]] = [{"offset": 1}, {"limit": 51}]
		for values in invalid_pages:
			self.assertEqual(self.listing(**values).status_code, 400)
		self.assertEqual(self.listing(self.filters(name=["like", "%"])).status_code, 400)
		with patch.object(directory, "MAX_DIRECTORY_CANDIDATES", 0):
			self.assertEqual(self.listing().status_code, 400)
		self.login_actor(source_fixture.MAKERS[0])
		self.denied(self.record())
		self.denied(self.listing())
		self.denied(self.listing(endpoint="eligible_sources"))

	def test_rejected_candidate_errors_do_not_contaminate_successful_empty_directory(self):
		def forbidden(_name):
			raise_ione_error("PERMISSION_DENIED")

		with patch.object(directory, "collect_release", side_effect=forbidden):
			response = self.listing()
			self.assertEqual(self.success(response)["items"], [])
			self.assertNotIn("ione_error", response.get_json())
			self.assertNotIn("X-Ione-Error-Code", response.headers)

	def test_auditor_can_read_complete_source_but_cannot_prepare_financial_confirmation(self):
		frappe.set_user("Administrator")
		had_auditor = "HRP Auditor" in frappe.get_roles(ACTOR)
		try:
			user = frappe.get_doc("User", ACTOR)
			user.add_roles("HRP Auditor")
			user.remove_roles("HRP Budget Manager")
			frappe.local.db.commit()
			self.login_actor(ACTOR)
			self.success(self.record())
			self.assertEqual(len(self.success(self.listing())["items"]), 1)
			self.denied(self.preview())
		finally:
			with source_fixture.committed_test_changes(self):
				user = frappe.get_doc("User", ACTOR)
				user.add_roles("HRP Budget Manager")
				if not had_auditor:
					user.remove_roles("HRP Auditor")

	@contextmanager
	def revoke_on_projection(self, revoke, *, final_only=False, after_commit=None):
		original = directory.public_release
		refresh = directory.refresh_statement_actor
		fired = False
		final = False

		def final_refresh():
			nonlocal final
			final = True
			return refresh()

		def project(doc):
			nonlocal fired
			result = original(doc)
			if not fired and (not final_only or final):
				fired = True
				primary = frappe.local.db
				with self.secondary_connection():
					frappe.set_user("Administrator")
					try:
						revoke()
						self._secondary_connection.commit()
					finally:
						self._secondary_connection.rollback()
				self.assertIs(frappe.local.db, primary)
				frappe.set_user(ACTOR)
				if after_commit:
					after_commit()
			return result

		with (
			patch.object(directory, "public_release", side_effect=project),
			patch.object(directory, "refresh_statement_actor", side_effect=final_refresh),
		):
			yield
		self.assertTrue(fired)

	def test_source_share_revocation_mid_directory_denies_all_results(self):
		try:
			with self.revoke_on_projection(
				lambda: frappe.share.remove("HRP Hospital", source_fixture.HOSPITAL, ACTOR)
			):
				self.denied(self.listing())
		finally:
			with source_fixture.committed_test_changes(self):
				share_test_records("HRP Hospital", (source_fixture.HOSPITAL,), (ACTOR,))

	def test_final_cached_source_success_cannot_hide_committed_share_revocation(self):
		try:
			with self.revoke_on_projection(
				lambda: frappe.share.remove("HRP Hospital", source_fixture.HOSPITAL, ACTOR), final_only=True
			):
				self.denied(self.listing(), "IONE-CORE-0005", (409,))
		finally:
			with source_fixture.committed_test_changes(self):
				share_test_records("HRP Hospital", (source_fixture.HOSPITAL,), (ACTOR,))

	def test_current_field_revocation_rejects_selected_record(self):
		setters = []

		def revoke():
			setters.append(
				make_property_setter(
					RELEASE, "total_amount", "permlevel", 2, "Int", validate_fields_for_doctype=False
				).name
			)

		try:
			with self.revoke_on_projection(revoke):
				self.denied(self.record())
		finally:
			with source_fixture.committed_test_changes(self):
				for name in setters:
					frappe.delete_doc("Property Setter", name, ignore_permissions=True)
					self.assertFalse(frappe.db.exists("Property Setter", name))
				frappe.clear_cache(doctype=RELEASE)

	def test_current_roles_override_poisoned_redis_before_returning_record(self):
		old = list(frappe.get_roles(ACTOR))

		def revoke():
			frappe.get_doc("User", ACTOR).remove_roles("HRP Budget Manager")
			frappe.db.after_commit.add(lambda: frappe.cache.hset("roles", ACTOR, old))

		try:
			with self.revoke_on_projection(
				revoke, after_commit=lambda: self.assertIn("HRP Budget Manager", frappe.get_roles(ACTOR))
			):
				self.denied(self.record())
		finally:
			with source_fixture.committed_test_changes(self):
				frappe.get_doc("User", ACTOR).add_roles("HRP Budget Manager")
			frappe.cache.hdel("roles", ACTOR)

	def test_current_native_cost_center_permissions_override_poisoned_redis(self):
		frappe.set_user("Administrator")
		centers = tuple(
			frappe.db.get_value(
				"HRP Organization Mapping", {"organization_unit": self.units[code]}, "cost_center"
			)
			for code in ("A", "B")
		)
		permissions = []
		try:
			for center in centers:
				permissions.append(
					frappe.get_doc(
						dict(
							doctype="User Permission",
							user=ACTOR,
							allow="Cost Center",
							for_value=center,
							apply_to_all_doctypes=1,
							hide_descendants=1,
						)
					)
					.insert(ignore_permissions=True)
					.name
				)
				frappe.share.remove("Cost Center", center, ACTOR)
			frappe.local.db.commit()
			self.login_actor(ACTOR)
			self.success(self.record())
			old = deepcopy(get_user_permissions(ACTOR))

			def revoke():
				frappe.delete_doc("User Permission", permissions[1], ignore_permissions=True)
				frappe.db.after_commit.add(lambda: frappe.cache.hset("user_permissions", ACTOR, old))

			with self.revoke_on_projection(
				revoke, after_commit=lambda: self.assertEqual(get_user_permissions(ACTOR), old)
			):
				self.denied(self.record())
		finally:
			with source_fixture.committed_test_changes(self):
				for name in permissions:
					if frappe.db.exists("User Permission", name):
						frappe.delete_doc("User Permission", name, ignore_permissions=True)
					self.assertFalse(frappe.db.exists("User Permission", name))
				share_test_records("Cost Center", centers, (ACTOR,))
			frappe.cache.hdel("user_permissions", ACTOR)

	def test_read_only_connection_preserves_primary_writes_callbacks_and_session(self):
		frappe.set_user("Administrator")
		module = load_module_registry().modules[0].module
		old = frappe.db.get_value("HRP Module Setting", module, "workspace_route")
		frappe.db.set_value("HRP Module Setting", module, "workspace_route", "COD033-directory-primary")
		frappe.set_user(ACTOR)
		primary, session = frappe.local.db, frappe.local.session
		callbacks = []
		manager = primary.after_commit
		primary.after_commit.add(lambda: callbacks.append(True))
		observed = []
		original = directory.ReadReleaseRecord.perform

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
			with patch.object(directory.ReadReleaseRecord, "perform", new=perform):
				release_api.get_record(self.release)
			self.assertEqual(observed, [(False, 1, old)])
			self.assertIs(frappe.local.db, primary)
			self.assertIs(frappe.local.session, session)
			self.assertIs(primary.after_commit, manager)
			self.assertEqual(callbacks, [])
			self.assertEqual(
				frappe.db.get_value("HRP Module Setting", module, "workspace_route"),
				"COD033-directory-primary",
			)
		finally:
			primary.rollback()
		self.assertEqual(frappe.db.get_value("HRP Module Setting", module, "workspace_route"), old)

	def test_reads_preserve_web_session_for_confirmation_replay_and_reconfirmation(self):
		before = self.TEST_CLIENT.get_cookie("sid").value
		self.success(self.record())
		self.assertTrue(compare_digest(before, self.TEST_CLIENT.get_cookie("sid").value))
		preview = self.success(self.preview())
		key = "directory-confirm:" + self.release
		first = self.success(self.confirm(preview, key=key))
		self.assertEqual(first["revision"], 2)
		replayed = self.success(self.confirm(preview, key=key))
		self.assertTrue(replayed["idempotency_replayed"])
		self.assertEqual(replayed["decision"], first["decision"])
		self.assertEqual(self.success(self.record())["revision"], 2)
		second = self.success(self.confirm(self.success(self.preview(revision=2))))
		self.assertEqual(second["revision"], 3)
		current = self.success(self.record())
		self.assertEqual(
			(current["status"], current["authorization_decision"]), ("Authorized", second["decision"])
		)
		self.assertNotEqual(first["decision"], second["decision"])
		self.denied(self.confirm(preview, key=key), "IONE-CORE-0005", (409,))
		self.assertEqual(frappe.db.count(LEDGER, {"voucher_no": self.release}), 0)
