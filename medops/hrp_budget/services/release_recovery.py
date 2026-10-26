from __future__ import annotations

import frappe
from frappe.utils import get_datetime, now_datetime

from medops.common.budget_ledger import RELEASE, RELEASE_ENTRY, ReleaseExecutionAuthorization
from medops.common.domain_service import fingerprint_json
from medops.common.integration_outbox import (
	OUTBOX_DEFINITION,
	RETIREMENT_ROLES,
	OutboxRetirement,
	outbox_retirement,
)
from medops.common.transactional_message import normalize_payload
from medops.hrp_budget.services.release_evidence import release_authorization
from medops.hrp_budget.services.release_execution import require_current_read_transaction
from medops.hrp_integration.services.outbox import RetireIntegrationOutbox
from medops.services.errors import raise_ione_error, require_roles
from medops.services.transactional_message import RetireOutboxMessage, read_message_document

SUPERSEDED = "MEDOPS-RELEASE-AUTHORIZATION-SUPERSEDED"
EXHAUSTED = "MEDOPS-RELEASE-LEASE-EXHAUSTED"


def historical_authority(message, *, lock_parent=False):
	"""Private historical integrity only; never grants visibility or financial authority.

	A read-only projection uses the original immutable decision epoch, so an old
	message can be retired without pretending it authorizes the current epoch.
	The projection is never inserted, saved or issued a financial write capability.
	"""
	payload, _, _ = normalize_payload(message.payload_json)
	authority = ReleaseExecutionAuthorization.from_payload(payload)
	if lock_parent:
		if not frappe.db.get_value(RELEASE, authority.release, "name", for_update=True, wait=False):
			raise_ione_error("RESOURCE_NOT_FOUND")
	parent = frappe.get_doc(RELEASE, authority.release, for_update=lock_parent)
	current = release_authorization(parent)
	decision = frappe.get_doc(RELEASE_ENTRY, authority.decision)
	projection = frappe.get_doc(
		dict(
			parent.as_dict(),
			status="Authorized",
			docstatus=0,
			revision=authority.authorization_revision,
			authorization_revision=authority.authorization_revision,
			authorization_decision=authority.decision,
			authorized_by=authority.actor,
			authorized_at=decision.recorded_at,
			outbox_message=message.name,
			posted_at=None,
			ledger_count=0,
			execution_digest=None,
		)
	)
	release_authorization(projection)
	if int(parent.authorization_revision) < authority.authorization_revision or (
		int(parent.authorization_revision) == authority.authorization_revision
		and (parent.outbox_message != message.name or current.name != authority.decision)
	):
		raise_ione_error("CONFIGURATION_INVALID")
	return authority, parent


def recover_release_message(name):
	"""Own only message lifecycle, locking parent -> message like the financial writer."""
	require_roles(RETIREMENT_ROLES)
	require_current_read_transaction()
	initial = read_message_document(OUTBOX_DEFINITION, name)
	if initial.status in {"Delivered", "Dead Letter"}:
		return initial, None, dict(message=name, status="Skipped")
	_, parent = historical_authority(initial, lock_parent=True)
	message = read_message_document(OUTBOX_DEFINITION, name, lock=True)
	authority, checked_parent = historical_authority(message)
	if checked_parent.name != parent.name:
		raise_ione_error("CONFLICT")
	reason = None
	if int(parent.authorization_revision) > authority.authorization_revision:
		reason = SUPERSEDED
	elif (
		message.status == "Processing"
		and int(message.attempt_count) == OUTBOX_DEFINITION.max_attempts
		and get_datetime(message.lease_expires_at) <= now_datetime()
	):
		reason = EXHAUSTED
	if reason is None:
		return message, parent, None
	expected = OutboxRetirement(
		message.name, message.payload_hash, int(message.attempt_count), message.status, reason
	)
	with outbox_retirement(expected):
		result = RetireIntegrationOutbox().execute(
			RetireOutboxMessage(message.name, message.payload_hash, reason),
			idempotency_key="budget-release-retired:" + fingerprint_json([name, reason]),
		)
	return message, parent, result.result
