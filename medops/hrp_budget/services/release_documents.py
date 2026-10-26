from __future__ import annotations

from decimal import Decimal, localcontext

import frappe
from frappe.utils import get_datetime, get_time

from medops.common.budget_consolidation import MAX_CONSOLIDATED_LINES, consolidation_digest
from medops.common.budget_ledger import BUDGET_LEDGER_DEFINITION, LEDGER, RELEASE, RELEASE_ENTRY
from medops.common.budget_release_control import release_mutation, release_record
from medops.hrp_budget.services.release_evidence import validate_new_release_evidence
from medops.services.budget_documents import ControlledBudgetDocument
from medops.services.errors import raise_ione_error
from medops.services.immutable_ledger import ImmutableLedgerDocument


def same_value(field, actual, expected):
	if actual in (None, "") and expected in (None, ""):
		return True
	if field.fieldtype in {"Int", "Float", "Check"} and expected is None:
		return actual in (None, 0)
	if field.fieldtype == "Datetime" and actual and expected:
		return get_datetime(actual) == get_datetime(expected)
	if field.fieldtype == "Time" and actual and expected:
		return get_time(actual) == get_time(expected)
	if field.fieldtype == "Date" and actual and expected:
		return str(actual) == str(expected)
	return actual == expected


def require_release_record(doc, operations):
	mutation, record = release_mutation(), release_record()
	if not mutation or mutation.operation not in operations or not record or record.doctype != doc.doctype:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	if doc.doctype == RELEASE:
		identity = doc.identity_key
	elif doc.doctype == RELEASE_ENTRY:
		identity = doc.release
	else:
		identity = doc.voucher_no
	if identity != mutation.facts.identity_key:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	for field in doc.meta.fields:
		if field.fieldtype in {"Section Break", "Column Break", "Tab Break"}:
			continue
		if not same_value(field, doc.get(field.fieldname), record.values.get(field.fieldname)):
			raise_ione_error("CONFIGURATION_INVALID")
	return mutation


class BudgetReleaseDocument(ControlledBudgetDocument):
	def before_validate(self):
		require_release_record(self, {"prepare", "revise", "confirm", "post"})

	def validate(self):
		mutation = require_release_record(self, {"prepare", "revise", "confirm", "post"})
		facts = mutation.facts
		if self.name != facts.identity_key or self.intent_digest != facts.intent_digest:
			raise_ione_error("CONFIGURATION_INVALID")
		for key, expected in facts.facts().items():
			field = self.meta.get_field(key)
			if field is None or not same_value(field, self.get(key), expected):
				raise_ione_error("CONFIGURATION_INVALID")
		previous = self.get_doc_before_save()
		if mutation.operation == "prepare":
			if (
				previous
				or not self.is_new()
				or int(self.revision) != 1
				or self.status != "Prepared"
				or self.docstatus
			):
				raise_ione_error("INVALID_STATE_TRANSITION")
		else:
			if not previous or previous.docstatus or int(previous.revision) != mutation.expected_revision:
				raise_ione_error("CONFLICT")
			if int(self.revision) != mutation.expected_revision + 1:
				raise_ione_error("CONFIGURATION_INVALID")
			if mutation.operation == "revise" and (
				previous.status != "Prepared" or self.status != "Prepared" or self.docstatus
			):
				raise_ione_error("INVALID_STATE_TRANSITION")
			if mutation.operation == "confirm":
				if (
					previous.status not in {"Prepared", "Authorized"}
					or self.status != "Authorized"
					or self.docstatus
				):
					raise_ione_error("INVALID_STATE_TRANSITION")
				if self.authorized_by != frappe.session.user or int(self.authorization_revision) != int(
					self.revision
				):
					raise_ione_error("CONFIGURATION_INVALID")
				if not self.authorization_decision or not self.authorized_at or not self.outbox_message:
					raise_ione_error("CONFIGURATION_INVALID")
			if mutation.operation == "post":
				if previous.status != "Authorized" or self.status != "Posted" or self.docstatus != 1:
					raise_ione_error("INVALID_STATE_TRANSITION")
				if self.authorized_by != frappe.session.user or int(self.authorization_revision) != int(
					previous.revision
				):
					raise_ione_error("CONFIGURATION_INVALID")
				self._immutable(
					tuple(
						field.fieldname
						for field in self.meta.fields
						if field.fieldname
						not in {"status", "revision", "posted_at", "ledger_count", "execution_digest"}
					)
				)
				self.validate_completed_batch()
		if self.status == "Prepared" and any(
			self.get(field)
			for field in (
				"authorized_by",
				"authorized_at",
				"authorization_revision",
				"authorization_decision",
				"outbox_message",
				"posted_at",
				"ledger_count",
				"execution_digest",
			)
		):
			raise_ione_error("CONFIGURATION_INVALID")
		if self.status != "Posted" and any(
			self.get(field) for field in ("posted_at", "ledger_count", "execution_digest")
		):
			raise_ione_error("CONFIGURATION_INVALID")

	def validate_completed_batch(self):
		rows = frappe.get_all(
			LEDGER,
			filters={"voucher_type": RELEASE, "voucher_no": self.name},
			fields=["entry_key", "source_hash"],
			order_by="entry_key asc",
			limit_page_length=MAX_CONSOLIDATED_LINES + 1,
		)
		if (
			not self.posted_at
			or len(rows) != int(self.line_count)
			or len(rows) != int(self.ledger_count)
			or len(rows) > MAX_CONSOLIDATED_LINES
			or consolidation_digest([dict(row) for row in rows]) != self.execution_digest
		):
			raise_ione_error("CONFIGURATION_INVALID")

	def before_save(self):
		require_release_record(self, {"prepare", "revise", "confirm"})

	def before_submit(self):
		require_release_record(self, {"post"})

	def db_insert(self, *args, **kwargs):
		require_release_record(self, {"prepare"})
		super().db_insert(*args, **kwargs)

	def db_update(self, *args, **kwargs):
		require_release_record(self, {"revise", "confirm", "post"})
		super().db_update(*args, **kwargs)

	def db_set(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")


class BudgetReleaseEntryDocument(ControlledBudgetDocument):
	def before_insert(self):
		require_release_record(self, {"preview", "confirm"})

	def validate(self):
		mutation = require_release_record(self, {"preview", "confirm"})
		if not self.is_new() or self.docstatus or self.actor != frappe.session.user:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if (
			self.action != mutation.facts.operation
			or int(self.release_revision) != mutation.expected_revision
		):
			raise_ione_error("CONFIGURATION_INVALID")
		if self.intent_digest != mutation.facts.intent_digest:
			raise_ione_error("CONFIGURATION_INVALID")
		if self.event_kind != ("Confirmation" if mutation.operation == "preview" else "Decision"):
			raise_ione_error("CONFIGURATION_INVALID")
		validate_new_release_evidence(self)

	def db_insert(self, *args, **kwargs):
		require_release_record(self, {"preview", "confirm"})
		super().db_insert(*args, **kwargs)

	def db_update(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_set(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")


class BudgetLedgerDocument(ImmutableLedgerDocument):
	ledger_definition = BUDGET_LEDGER_DEFINITION

	def before_insert(self):
		require_release_record(self, {"post"})
		super().before_insert()

	def validate(self):
		require_release_record(self, {"post"})
		super().validate()
		if self.name != self.entry_key or self.entry_type != "Allocation":
			raise_ione_error("CONFIGURATION_INVALID")
		with localcontext() as context:
			context.prec = 48
			if Decimal(self.amount) != Decimal(self.debit) - Decimal(self.credit):
				raise_ione_error("CONFIGURATION_INVALID")

	def db_insert(self, *args, **kwargs):
		require_release_record(self, {"post"})
		super().db_insert(*args, **kwargs)
