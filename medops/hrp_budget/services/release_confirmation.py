from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import asdict
from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

from medops.common.budget_ledger import (
	RELEASE,
	RELEASE_ENTRY,
	RELEASE_EVENT_DESTINATION,
	RELEASE_EVENT_SOURCE,
	RELEASE_EVENT_TYPE,
	ReleaseExecutionAuthorization,
	ReleasePreviewRequest,
)
from medops.common.budget_release_control import expected_release_record, release_write
from medops.common.budget_workflow import CONFIRMATION_SECONDS, HumanConfirmation
from medops.common.confirmation_privacy import ConfirmationProof
from medops.common.domain_service import DomainServiceDefinition, fingerprint_json
from medops.common.integration_outbox import outbox_publication
from medops.common.transactional_message import normalize_payload
from medops.hrp_budget.jobs.dispatch import dispatch_authorized_release
from medops.hrp_budget.services.approval import require_web_session, session_digest
from medops.hrp_budget.services.planning import execution_payload
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.release import (
	assert_reversible,
	lock_release_source,
	release_result,
	require_operational_release,
	require_release_actor,
	require_release_scope,
)
from medops.hrp_budget.services.release_access import get_release
from medops.hrp_budget.services.release_evidence import (
	release_authorization,
	release_evidence_values,
	validate_release_evidence,
)
from medops.hrp_budget.services.release_sources import stored_release_facts
from medops.hrp_integration.services.outbox import PublishIntegrationOutbox
from medops.hrp_workflow_authorization.services.business_rules import enforce_business_rules
from medops.services.audit_context import emit_audit_event
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error
from medops.services.transactional_message import MessageEnvelope


def financial_session_digest(name):
	return session_digest("budget-release:" + name)


def require_financial_actor(doc):
	require_release_actor()
	facts = stored_release_facts(doc)
	require_release_scope(facts, "write", "submit")
	require_operational_release(facts)
	if facts.operation == "Reverse":
		assert_reversible(get_release(facts.reversal_of))
	return facts


def require_open_intent(doc, expected_revision):
	if (
		doc.docstatus
		or doc.status not in {"Prepared", "Authorized"}
		or int(doc.revision) != expected_revision
	):
		raise_ione_error("CONFLICT")


def checked_release_confirmation(name, proof):
	if not frappe.db.exists(RELEASE_ENTRY, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	row = frappe.get_doc(RELEASE_ENTRY, name)
	validate_release_evidence(row)
	if (
		row.event_kind != "Confirmation"
		or row.actor != frappe.session.user
		or not hmac.compare_digest(str(row.confirmation_hash), hashlib.sha256(proof.encode()).hexdigest())
		or not hmac.compare_digest(str(row.session_digest), financial_session_digest(row.release))
	):
		raise_ione_error("PERMISSION_DENIED")
	return row


def append_release_event(doc, facts, kind, proof_hash, session_hash, expiry, *, confirmation=None):
	values = dict(
		release=doc.name,
		company=doc.company,
		hospital=doc.hospital,
		organization_unit=doc.organization_unit,
		event_kind=kind,
		action=doc.operation,
		actor=frappe.session.user,
		release_revision=int(doc.revision),
		intent_digest=doc.intent_digest,
		source_snapshot_digest=doc.source_snapshot_digest,
		reason=doc.reason,
		recorded_at=now_datetime(),
		expires_at=expiry,
		confirmation_hash=proof_hash,
		session_digest=session_hash,
		confirmation=confirmation,
		revision_key=fingerprint_json([doc.name, int(doc.revision)]) if kind == "Decision" else None,
	)
	row = frappe.get_doc(dict(doctype=RELEASE_ENTRY, **values))
	values["event_digest"] = row.event_digest = fingerprint_json(release_evidence_values(row))
	with expected_release_record(RELEASE_ENTRY, values):
		row.insert(ignore_permissions=True)
	return row


class PrepareReleaseConfirmation(DomainService[ReleasePreviewRequest]):
	definition = DomainServiceDefinition(
		name="hrp_budget.release.preview", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		require_web_session()
		require_release_actor()

	def request_payload(self, command):
		return asdict(command)

	def validate(self, command):
		require_financial_actor(get_release(command.release))

	def authorize_result(self, result):
		row = checked_release_confirmation(str(result["confirmation"]), ConfirmationProof(result["proof"]))
		doc = get_release(row.release)
		require_financial_actor(doc)
		require_open_intent(doc, int(row.release_revision))
		if get_datetime(row.expires_at) <= now_datetime():
			raise_ione_error("CONFLICT")

	def perform(self, command):
		doc = lock_release_source(command.release)
		facts = require_financial_actor(doc)
		require_open_intent(doc, command.expected_revision)
		enforce_business_rules(doc, "release")
		proof = ConfirmationProof(secrets.token_urlsafe(32))
		expiry = now_datetime() + timedelta(seconds=CONFIRMATION_SECONDS)
		with release_write("preview", facts, command.expected_revision):
			row = append_release_event(
				doc,
				facts,
				"Confirmation",
				hashlib.sha256(proof.encode()).hexdigest(),
				financial_session_digest(doc.name),
				expiry,
			)
		return dict(
			confirmation=row.name,
			proof=proof,
			expires_at=str(expiry),
			release=doc.name,
			release_revision=int(doc.revision),
			action=doc.operation,
			reason=doc.reason,
			posting_date=str(doc.posting_date),
			total_amount=doc.total_amount,
			currency=doc.currency,
			line_count=int(doc.line_count),
			intent_digest=doc.intent_digest,
		)


class ConfirmReleaseAuthorization(DomainService[HumanConfirmation]):
	definition = DomainServiceDefinition(
		name="hrp_budget.release.confirm", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		require_web_session()
		require_release_actor()
		if command.acknowledge is not True:
			raise_ione_error("INVALID_REQUEST")

	def request_payload(self, command):
		return dict(
			confirmation=command.confirmation,
			proof_hash=hashlib.sha256(command.proof.encode()).hexdigest(),
			acknowledge=command.acknowledge,
		)

	def validate(self, command):
		row = checked_release_confirmation(command.confirmation, command.proof)
		require_financial_actor(get_release(row.release))

	def authorize_result(self, result):
		doc = get_release(str(result["name"]))
		require_financial_actor(doc)
		decision = release_authorization(doc)
		if decision.name != result["decision"] or decision.actor != frappe.session.user:
			raise_ione_error("CONFLICT")

	def perform(self, command):
		original = checked_release_confirmation(command.confirmation, command.proof)
		doc = lock_release_source(original.release)
		facts = require_financial_actor(doc)
		require_open_intent(doc, int(original.release_revision))
		if original.intent_digest != doc.intent_digest or get_datetime(original.expires_at) <= now_datetime():
			raise_ione_error("CONFLICT")
		if frappe.db.get_value(RELEASE_ENTRY, {"confirmation": original.name}, "name", for_update=True):
			raise_ione_error("CONFLICT")
		enforce_business_rules(doc, "release")
		with release_write("confirm", facts, int(doc.revision)):
			decision = append_release_event(
				doc,
				facts,
				"Decision",
				original.confirmation_hash,
				original.session_digest,
				original.expires_at,
				confirmation=original.name,
			)
			authority = ReleaseExecutionAuthorization.from_payload(
				dict(
					release=doc.name,
					authorization_revision=int(doc.revision) + 1,
					intent_digest=doc.intent_digest,
					decision=decision.name,
					actor=frappe.session.user,
				)
			)
			payload = asdict(authority)
			_, _, payload_hash = normalize_payload(payload)
			publication = dict(
				event_id=authority.event_id,
				event_type=RELEASE_EVENT_TYPE,
				source=RELEASE_EVENT_SOURCE,
				destination=RELEASE_EVENT_DESTINATION,
				payload_hash=payload_hash,
				reference_doctype=RELEASE,
				reference_name=doc.name,
			)
			with outbox_publication(publication):
				message = PublishIntegrationOutbox().execute(
					MessageEnvelope(
						event_id=authority.event_id,
						event_type=RELEASE_EVENT_TYPE,
						source=RELEASE_EVENT_SOURCE,
						destination=RELEASE_EVENT_DESTINATION,
						occurred_at=str(decision.recorded_at),
						payload=payload,
						reference_doctype=RELEASE,
						reference_name=doc.name,
					),
					idempotency_key="release-authorization:" + authority.event_id,
				)
			values = {field.fieldname: doc.get(field.fieldname) for field in doc.meta.fields}
			values.update(
				status="Authorized",
				revision=authority.authorization_revision,
				authorization_revision=authority.authorization_revision,
				authorized_by=frappe.session.user,
				authorized_at=decision.recorded_at,
				authorization_decision=decision.name,
				outbox_message=message.result["message"],
			)
			with expected_release_record(RELEASE, values):
				doc.update(values)
				doc.save(ignore_permissions=True)
		emit_audit_event("budget_release_authorized", logger_name="medops.budget", revision=int(doc.revision))
		return dict(**release_result(doc), decision=decision.name)


def prepare_release_confirmation(command, *, idempotency_key=None, correlation_id=None):
	return execution_payload(
		PrepareReleaseConfirmation().execute(
			command, idempotency_key=idempotency_key, correlation_id=correlation_id
		)
	)


def confirm_release_authorization(command, *, idempotency_key=None, correlation_id=None):
	result = ConfirmReleaseAuthorization().execute(
		command, idempotency_key=idempotency_key, correlation_id=correlation_id
	)
	name = str(result.result["name"])
	dispatch_authorized_release(
		name,
		frappe.db.get_value(RELEASE, name, "outbox_message"),
		int(str(result.result["revision"])),
		correlation_id=result.correlation_id,
	)
	return execution_payload(result)
