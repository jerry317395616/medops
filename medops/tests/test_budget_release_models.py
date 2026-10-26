from __future__ import annotations

from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.budget_ledger import BUDGET_LEDGER_DEFINITION, LEDGER, RELEASE, RELEASE_ENTRY
from medops.common.error_catalog import IoneApplicationError
from medops.common.integration_outbox import OUTBOX, OUTBOX_DEFINITION
from medops.hrp_integration.services.outbox import PublishIntegrationOutbox
from medops.services.audit_context import service_audit_scope
from medops.services.immutable_ledger import append_immutable_entry, validate_immutable_ledger_doctype
from medops.services.transactional_message import MessageEnvelope, validate_message_box_doctype
from medops.setup.budget_ledger import ensure_budget_ledger


class TestBudgetReleaseModels(IntegrationTestCase):
	def test_four_runtime_models_have_precise_immutable_contracts_and_no_seed(self):
		before = {name: frappe.db.count(name) for name in (RELEASE, RELEASE_ENTRY, LEDGER, OUTBOX)}
		self.assertTrue(validate_immutable_ledger_doctype(BUDGET_LEDGER_DEFINITION))
		self.assertTrue(validate_message_box_doctype(OUTBOX_DEFINITION))
		self.assertEqual(ensure_budget_ledger()["financial_seeds"], 0)
		self.assertEqual(before, {name: frappe.db.count(name) for name in before})
		for name in before:
			for permission in frappe.get_meta(name).permissions:
				for action in ("create", "write", "delete", "submit", "cancel", "amend", "import"):
					self.assertFalse(permission.get(action))

	def test_native_insert_cannot_create_any_financial_or_publication_record(self):
		for name in (RELEASE, RELEASE_ENTRY, LEDGER, OUTBOX):
			before = frappe.db.count(name)
			with self.subTest(doctype=name), self.assertRaises(IoneApplicationError) as raised:
				frappe.get_doc(dict(doctype=name, identity_key="a" * 64, entry_key="b" * 64)).insert(
					ignore_permissions=True
				)
			self.assertEqual(raised.exception.code, "IONE-CORE-0008")
			self.assertEqual(frappe.db.count(name), before)

	def test_generic_ledger_backend_role_does_not_grant_financial_posting(self):
		before = frappe.db.count(LEDGER)
		with service_audit_scope(), self.assertRaises(IoneApplicationError) as raised:
			append_immutable_entry(
				BUDGET_LEDGER_DEFINITION,
				dict(
					amount_schema_version=2,
					currency_precision=9,
					amount="0.000000000",
					debit="0.000000000",
					credit="0.000000000",
				),
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")
		self.assertEqual(frappe.db.count(LEDGER), before)

	def test_generic_publisher_does_not_grant_domain_publication(self):
		before = frappe.db.count(OUTBOX)
		command = MessageEnvelope(
			event_id="cod033-" + uuid4().hex,
			event_type="medops.budget.authorized",
			source="medops.budget",
			destination="medops.budget.executor",
			occurred_at="2026-10-26 13:00:00",
			payload_schema_version=1,
			payload={"reference": "synthetic"},
		)
		with self.assertRaises(IoneApplicationError) as raised:
			PublishIntegrationOutbox().execute(command, idempotency_key="cod033-" + uuid4().hex)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")
		self.assertEqual(frappe.db.count(OUTBOX), before)

	def test_low_level_native_updates_require_the_same_owned_boundary(self):
		for name in (RELEASE, RELEASE_ENTRY, LEDGER, OUTBOX):
			doc = frappe.get_doc(dict(doctype=name, name="missing-synthetic"))
			with self.subTest(doctype=name), self.assertRaises(IoneApplicationError) as raised:
				doc.db_set("status", "injected")
			self.assertEqual(raised.exception.code, "IONE-CORE-0008")
			with self.assertRaises(IoneApplicationError):
				doc.db_update()
