from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe.permissions import ALL_USER_ROLE, AUTOMATIC_ROLES, GUEST_ROLE, SYSTEM_USER_ROLE
from frappe.utils import get_datetime, get_time, now_datetime
from frappe.utils.user import is_system_user

from medops.common.budget_consolidation import MAX_CONSOLIDATED_LINES, consolidation_digest
from medops.common.budget_ledger import (
	BUDGET_LEDGER_DEFINITION,
	LEDGER,
	RELEASE,
	ReleaseExecutionAuthorization,
	allocation_values,
	reversal_allocation_values,
)
from medops.common.budget_release_control import execution_lease, expected_release_record, release_write
from medops.common.domain_service import DomainServiceDefinition
from medops.common.integration_outbox import OUTBOX_DEFINITION
from medops.common.transactional_message import normalize_payload, processing_token_matches
from medops.hrp_budget.services.access_graph import access_graph_cache
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.release import lock_release_source, release_result
from medops.hrp_budget.services.release_access import get_release
from medops.hrp_budget.services.release_confirmation import require_financial_actor
from medops.hrp_budget.services.release_documents import same_value
from medops.hrp_budget.services.release_evidence import release_authorization
from medops.hrp_budget.services.release_sources import stored_release_facts
from medops.hrp_integration.services.outbox import CompleteIntegrationOutbox
from medops.hrp_workflow_authorization.services.business_rules import enforce_business_rules
from medops.services.audit_context import emit_audit_event
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error
from medops.services.immutable_ledger import append_immutable_entry, reverse_immutable_entry
from medops.services.transactional_message import CompleteMessage, read_message_document

RELEASE_WORKER = "medops.budget.release.worker"
LEASE_SECONDS = 3600
EXECUTION_TIMEOUT = 1800
AUTHORITY_ROOTS = (
	RELEASE,
	"HRP Budget Release Entry",
	LEDGER,
	"HRP Integration Outbox",
	"HRP Budget Approval",
	"HRP Budget Review",
	"HRP Budget Submission",
	"HRP Budget Plan",
	"HRP Budget Version",
	"HRP Business Dimension",
	"HRP Dimension Value",
	"HRP Funding Source",
	"HRP Hospital",
	"HRP Organization Version",
	"HRP Organization Unit",
	"HRP Organization Mapping",
	"Company",
	"Fiscal Year",
	"Currency",
	"Cost Center",
	"Account",
	"Project",
	"Workflow",
)


def require_current_read_transaction():
	if (
		str(frappe.db.sql("SELECT @@tx_isolation")[0][0]).upper().replace("_", "-") != "READ-COMMITTED"
		or frappe.db.auto_commit_on_many_writes
		or access_graph_cache() is not None
	):
		raise_ione_error("CONFIGURATION_INVALID")


def refresh_financial_authority(actor):
	"""Re-read this actor and reviewed source metadata in the current RC transaction.

	No old Redis role/User Permission or process metadata success may authorize
	this batch. Repeated at result validation, after the financial writes.
	"""
	require_current_read_transaction()
	frappe.set_user(actor)
	frappe.db.value_cache.clear()
	# Use the pinned native role semantics, but read the assignments directly.
	# Deleting a Redis key alone races with another old transaction repopulating
	# it. The current request-local value cannot be replaced by that transaction.
	if actor == "Administrator":
		roles = frappe.get_all("Role", pluck="name")
	else:
		roles = frappe.get_all(
			"Has Role",
			filters={"parenttype": "User", "parent": actor, "role": ["not in", list(AUTOMATIC_ROLES)]},
			pluck="role",
		)
		roles += [ALL_USER_ROLE, GUEST_ROLE]
		if is_system_user(actor):
			roles.append(SYSTEM_USER_ROLE)
	frappe.local.cache[frappe.cache.make_key("roles")] = {actor: roles}
	# A local negative entry asks the native User Permission loader to read DB,
	# retaining all native nested-set/applicable_for semantics without Redis reuse.
	frappe.local.cache[frappe.cache.make_key("user_permissions")] = {actor: None}
	pending = set(AUTHORITY_ROOTS) | set(
		frappe.get_all("User Permission", filters={"user": actor}, pluck="allow")
	)
	seen = set()
	while pending:
		name = pending.pop()
		if name in seen:
			continue
		seen.add(name)
		if len(seen) > 256:
			raise_ione_error("CONFIGURATION_INVALID")
		meta = frappe.get_meta(name, cached=False)
		pending.update(
			field.options
			for field in meta.fields
			if field.fieldtype in {"Table", "Table MultiSelect"}
			and field.options
			and field.options not in seen
		)


def message_authority(message):
	"""Private adapter integrity; financial visibility must still be checked as actor."""
	payload, _, _ = normalize_payload(message.payload_json)
	authority = ReleaseExecutionAuthorization.from_payload(payload)
	doc = frappe.get_doc(RELEASE, authority.release)
	decision = release_authorization(doc)
	if (
		doc.outbox_message != message.name
		or decision.name != authority.decision
		or decision.actor != authority.actor
		or int(doc.authorization_revision) != authority.authorization_revision
		or doc.intent_digest != authority.intent_digest
	):
		raise_ione_error("CONFLICT")
	return authority, doc


def require_execution(message=None):
	lease = execution_lease()
	if lease is None or lease.authority.actor != frappe.session.user:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	require_current_read_transaction()
	if lease.expires_at <= now_datetime():
		raise_ione_error("CONFLICT")
	if message is not None and (
		message.name != lease.message
		or message.status != "Processing"
		or int(message.attempt_count) != lease.attempt
		or message.claim_owner != RELEASE_WORKER
		or get_datetime(message.lease_expires_at) != lease.expires_at
		or not processing_token_matches(lease.processing_token, message.processing_token_hash)
	):
		raise_ione_error("CONFLICT")
	return lease


def verified_original_rows(original, facts):
	rows = frappe.get_all(
		LEDGER,
		filters={"voucher_type": RELEASE, "voucher_no": original.name},
		fields=["name"],
		order_by="entry_key asc",
		limit_page_length=MAX_CONSOLIDATED_LINES + 1,
	)
	if len(rows) != len(facts.snapshot.lines) or not rows:
		raise_ione_error("CONFIGURATION_INVALID")
	documents = {row.name: frappe.get_doc(LEDGER, row.name, for_update=True) for row in rows}
	clock = get_time(next(iter(documents.values())).posting_time).isoformat()
	for values in allocation_values(facts, posting_time=clock):
		doc = documents.get(values["entry_key"])
		if not doc:
			raise_ione_error("CONFIGURATION_INVALID")
		expected = dict(values, is_reversal=0, reversal_of=None)
		if any(
			not same_value(field, doc.get(field.fieldname), expected.get(field.fieldname))
			for field in doc.meta.fields
			if field.fieldtype not in {"Section Break", "Column Break", "Tab Break"}
		):
			raise_ione_error("CONFIGURATION_INVALID")
	return documents, clock


def write_complete_batch(doc, facts):
	clock = now_datetime().time().isoformat()
	proof = []
	if facts.operation == "Release":
		for values in allocation_values(facts, posting_time=clock):
			with expected_release_record(LEDGER, dict(values, is_reversal=0, reversal_of=None)):
				append_immutable_entry(BUDGET_LEDGER_DEFINITION, values)
			proof.append(dict(entry_key=values["entry_key"], source_hash=values["source_hash"]))
	else:
		original = get_release(facts.reversal_of, lock=True)
		original_facts = stored_release_facts(original)
		_, original_clock = verified_original_rows(original, original_facts)
		for values in reversal_allocation_values(
			original_facts, facts, original_posting_time=original_clock, posting_time=clock
		):
			overrides = {key: values[key] for key in BUDGET_LEDGER_DEFINITION.reversal_override_fields}
			with expected_release_record(LEDGER, values):
				reverse_immutable_entry(BUDGET_LEDGER_DEFINITION, values["reversal_of"], overrides)
			proof.append(dict(entry_key=values["entry_key"], source_hash=values["source_hash"]))
	if len(proof) != int(doc.line_count):
		raise_ione_error("CONFIGURATION_INVALID")
	return len(proof), consolidation_digest(sorted(proof, key=lambda row: row["entry_key"]))


@dataclass(frozen=True, slots=True)
class ExecuteRelease:
	message: str


class ExecuteBudgetRelease(DomainService[ExecuteRelease]):
	definition = DomainServiceDefinition(
		name="hrp_budget.release.execute", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def authorize(self, command):
		lease = require_execution()
		if command.message != lease.message:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		refresh_financial_authority(lease.authority.actor)
		if frappe.get_meta(RELEASE).queue_in_background:
			raise_ione_error("CONFIGURATION_INVALID")
		super().authorize(command)

	def request_payload(self, command):
		lease = require_execution()
		return dict(
			message=command.message,
			attempt=lease.attempt,
			authorization_revision=lease.authority.authorization_revision,
		)

	def validate(self, command):
		message = read_message_document(OUTBOX_DEFINITION, command.message)
		lease = require_execution(message)
		authority, _ = message_authority(message)
		if authority != lease.authority:
			raise_ione_error("CONFLICT")
		require_financial_actor(get_release(authority.release))

	def authorize_result(self, result):
		lease = require_execution()
		refresh_financial_authority(lease.authority.actor)
		doc = get_release(str(result["name"]))
		require_financial_actor(doc)
		if doc.status != "Posted" or doc.outbox_message != lease.message:
			raise_ione_error("CONFLICT")

	def perform(self, command):
		lease = require_execution()
		doc = lock_release_source(lease.authority.release)
		message = read_message_document(OUTBOX_DEFINITION, command.message, lock=True)
		require_execution(message)
		authority, _ = message_authority(message)
		if (
			authority != lease.authority
			or doc.status != "Authorized"
			or int(doc.revision) != authority.authorization_revision
		):
			raise_ione_error("CONFLICT")
		facts = require_financial_actor(doc)
		enforce_business_rules(doc, "post")
		with release_write("post", facts, int(doc.revision)):
			count, digest = write_complete_batch(doc, facts)
			# Complete and submit are part of this same transaction/savepoint.
			# Result validation then re-reads current grants and all sources again.
			require_execution(message)
			CompleteIntegrationOutbox().execute(
				CompleteMessage(message_name=message.name, processing_token=lease.processing_token),
				idempotency_key="budget-release-delivered:" + authority.event_id,
			)
			values = {field.fieldname: doc.get(field.fieldname) for field in doc.meta.fields}
			values.update(
				status="Posted",
				revision=int(doc.revision) + 1,
				posted_at=now_datetime(),
				ledger_count=count,
				execution_digest=digest,
			)
			with expected_release_record(RELEASE, values):
				doc.update(values)
				doc.flags.ignore_permissions = True
				doc.submit()
		emit_audit_event(
			"budget_release_posted", logger_name="medops.budget", line_count=count, revision=int(doc.revision)
		)
		return release_result(doc)
