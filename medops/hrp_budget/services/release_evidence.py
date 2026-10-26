from __future__ import annotations

from dataclasses import asdict

import frappe
from frappe.utils import get_datetime

from medops.common.budget_ledger import (
	RELEASE,
	RELEASE_ENTRY,
	RELEASE_EVENT_DESTINATION,
	RELEASE_EVENT_SOURCE,
	RELEASE_EVENT_TYPE,
	ReleaseExecutionAuthorization,
)
from medops.common.domain_service import fingerprint_json, normalize_sha256
from medops.common.integration_outbox import OUTBOX
from medops.common.transactional_message import message_record_name, normalize_payload
from medops.services.errors import raise_ione_error

RELEASE_EVIDENCE_FIELDS = (
	"release",
	"company",
	"hospital",
	"organization_unit",
	"event_kind",
	"action",
	"actor",
	"release_revision",
	"intent_digest",
	"source_snapshot_digest",
	"reason",
	"recorded_at",
	"expires_at",
	"confirmation_hash",
	"session_digest",
	"confirmation",
	"revision_key",
)


def release_evidence_values(doc):
	values = {key: doc.get(key) for key in RELEASE_EVIDENCE_FIELDS}
	for key in ("recorded_at", "expires_at"):
		values[key] = str(get_datetime(values[key])) if values[key] else None
	for key in ("confirmation", "revision_key"):
		values[key] = values[key] or None
	return values


def validate_release_evidence(doc):
	try:
		values = release_evidence_values(doc)
		for key in ("intent_digest", "source_snapshot_digest", "confirmation_hash", "session_digest"):
			normalize_sha256(values[key], label=key)
		if fingerprint_json(values) != doc.event_digest or get_datetime(doc.expires_at) <= get_datetime(
			doc.recorded_at
		):
			raise ValueError("financial confirmation evidence differs")
	except (TypeError, ValueError) as error:
		raise_ione_error("CONFIGURATION_INVALID", cause=error)


def validate_new_release_evidence(doc):
	validate_release_evidence(doc)
	parent = frappe.get_doc(RELEASE, doc.release)
	if (
		parent.docstatus
		or parent.status not in {"Prepared", "Authorized"}
		or int(parent.revision) != int(doc.release_revision)
	):
		raise_ione_error("CONFLICT")
	if doc.action != parent.operation or any(
		doc.get(key) != parent.get(key)
		for key in (
			"company",
			"hospital",
			"organization_unit",
			"intent_digest",
			"source_snapshot_digest",
			"reason",
		)
	):
		raise_ione_error("CONFIGURATION_INVALID")
	if doc.event_kind == "Confirmation":
		if doc.confirmation or doc.revision_key:
			raise_ione_error("CONFIGURATION_INVALID")
	elif doc.event_kind == "Decision":
		if not doc.confirmation or doc.revision_key != fingerprint_json(
			[doc.release, int(doc.release_revision)]
		):
			raise_ione_error("CONFIGURATION_INVALID")
		original = frappe.get_doc(RELEASE_ENTRY, doc.confirmation)
		validate_release_evidence(original)
		if original.event_kind != "Confirmation" or any(
			original.get(key) != doc.get(key)
			for key in (
				"release",
				"company",
				"hospital",
				"organization_unit",
				"action",
				"actor",
				"release_revision",
				"intent_digest",
				"source_snapshot_digest",
				"reason",
				"confirmation_hash",
				"session_digest",
			)
		):
			raise_ione_error("CONFIGURATION_INVALID")
		if get_datetime(original.expires_at) != get_datetime(doc.expires_at) or get_datetime(
			doc.recorded_at
		) < get_datetime(original.recorded_at):
			raise_ione_error("CONFIGURATION_INVALID")
	else:
		raise_ione_error("CONFIGURATION_INVALID")


def release_authorization(doc):
	if doc.status not in {"Authorized", "Posted"} or not doc.authorization_decision or not doc.outbox_message:
		raise_ione_error("INVALID_STATE_TRANSITION")
	decision = frappe.get_doc(RELEASE_ENTRY, doc.authorization_decision)
	validate_release_evidence(decision)
	if (
		decision.event_kind != "Decision"
		or decision.release != doc.name
		or decision.actor != doc.authorized_by
		or decision.action != doc.operation
		or int(doc.authorization_revision) != int(decision.release_revision) + 1
		or int(doc.revision) != int(doc.authorization_revision) + (1 if doc.status == "Posted" else 0)
		or int(doc.docstatus) != (1 if doc.status == "Posted" else 0)
		or get_datetime(doc.authorized_at) != get_datetime(decision.recorded_at)
		or any(
			decision.get(key) != doc.get(key)
			for key in (
				"company",
				"hospital",
				"organization_unit",
				"intent_digest",
				"source_snapshot_digest",
				"reason",
			)
		)
	):
		raise_ione_error("CONFIGURATION_INVALID")
	confirmation = frappe.get_doc(RELEASE_ENTRY, decision.confirmation)
	validate_release_evidence(confirmation)
	if (
		confirmation.event_kind != "Confirmation"
		or decision.revision_key != fingerprint_json([doc.name, int(decision.release_revision)])
		or any(
			confirmation.get(key) != decision.get(key)
			for key in (
				"release",
				"actor",
				"action",
				"release_revision",
				"intent_digest",
				"source_snapshot_digest",
				"reason",
				"confirmation_hash",
				"session_digest",
				"company",
				"hospital",
				"organization_unit",
			)
		)
		or get_datetime(confirmation.expires_at) != get_datetime(decision.expires_at)
		or get_datetime(decision.recorded_at) < get_datetime(confirmation.recorded_at)
	):
		raise_ione_error("CONFIGURATION_INVALID")
	validate_authorization_message(doc, decision)
	return decision


def validate_authorization_message(doc, decision):
	# Internal integrity proof only. Never return private payloads or leases from
	# this adapter, and never treat it as a grant to read a financial source.
	message = frappe.get_doc(OUTBOX, doc.outbox_message)
	try:
		authority = ReleaseExecutionAuthorization.from_payload(
			dict(
				release=doc.name,
				authorization_revision=int(doc.authorization_revision),
				intent_digest=doc.intent_digest,
				decision=decision.name,
				actor=decision.actor,
			)
		)
		payload, _, digest = normalize_payload(message.payload_json)
		if (
			payload != asdict(authority)
			or digest != message.payload_hash
			or message.name != message_record_name("outbox", authority.event_id)
			or message.event_id != authority.event_id
			or message.event_type != RELEASE_EVENT_TYPE
			or message.source != RELEASE_EVENT_SOURCE
			or message.destination != RELEASE_EVENT_DESTINATION
			or message.reference_doctype != RELEASE
			or message.reference_name != doc.name
			or int(message.payload_schema_version) != 1
			or get_datetime(message.occurred_at) != get_datetime(decision.recorded_at)
			or (doc.status == "Posted" and message.status != "Completed")
		):
			raise ValueError("durable financial message differs")
	except (TypeError, ValueError) as error:
		raise_ione_error("CONFIGURATION_INVALID", cause=error)
