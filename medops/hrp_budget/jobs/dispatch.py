"""Commit-only queue hints; financial authority remains in the owned worker."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Literal

import frappe
from frappe.utils import get_datetime, now_datetime

from medops.common.budget_ledger import (
	RELEASE,
	RELEASE_EVENT_DESTINATION,
	RELEASE_EVENT_SOURCE,
	RELEASE_EVENT_TYPE,
)
from medops.common.budget_release_control import (
	CLAIM_JOB,
	EXECUTE_JOB,
	EXECUTION_TIMEOUT,
	RELEASE_WORKER,
	claim_receipt_key,
)
from medops.common.domain_service import fingerprint_json
from medops.common.integration_outbox import OUTBOX
from medops.common.organization import normalize_positive_integer, normalize_reference
from medops.services.audit_context import emit_audit_event, ensure_audit_context, service_audit_scope


@dataclass(frozen=True, slots=True)
class _DispatchHint:
	kind: Literal["claim", "execute", "authorize"]
	message: str
	correlation_id: str
	receipt: str | None = None
	release: str | None = None
	revision: int | None = None


def _message(hint):
	row = frappe.db.get_value(
		OUTBOX,
		hint.message,
		[
			"event_type",
			"source",
			"destination",
			"reference_doctype",
			"reference_name",
			"status",
			"attempt_count",
			"claim_owner",
			"lease_expires_at",
			"available_at",
		],
		as_dict=True,
	)
	if not row or (
		row.event_type != RELEASE_EVENT_TYPE
		or row.source != RELEASE_EVENT_SOURCE
		or row.destination != RELEASE_EVENT_DESTINATION
		or row.reference_doctype != RELEASE
		or row.status not in {"Pending", "Failed", "Processing"}
	):
		return None
	parent = frappe.db.get_value(
		RELEASE,
		row.reference_name,
		["status", "docstatus", "authorization_revision", "outbox_message"],
		as_dict=True,
	)
	if not parent:
		return None
	if hint.kind == "authorize" and (
		row.reference_name != hint.release
		or parent.status != "Authorized"
		or int(parent.docstatus) != 0
		or int(parent.authorization_revision) != hint.revision
		or parent.outbox_message != hint.message
	):
		return None
	if hint.kind == "execute":
		if (
			row.status != "Processing"
			or row.claim_owner != RELEASE_WORKER
			or not row.lease_expires_at
			or get_datetime(row.lease_expires_at) <= now_datetime()
			or hint.receipt != claim_receipt_key(hint.message, row.attempt_count)
		):
			return None
	elif row.status in {"Pending", "Failed"} and get_datetime(row.available_at) > now_datetime():
		return None
	return row


def _enqueue_committed(hint):
	# Frappe enqueue(..., enqueue_after_commit=True) still contacts Redis for
	# deduplication before registering its callback. Invoke it only here, after
	# commit, and recheck persisted identity to reject savepoint rollback hints.
	try:
		if _message(hint) is None:
			return
		kwargs = dict(message=hint.message)
		if hint.kind == "execute":
			kwargs["receipt"] = hint.receipt
			method, timeout = EXECUTE_JOB, EXECUTION_TIMEOUT
			job_id = "medops-release-execute-" + fingerprint_json([hint.message, hint.receipt])
		else:
			method, timeout = CLAIM_JOB, 300
			job_id = "medops-release-claim-" + hint.message
		frappe.enqueue(
			method,
			queue="long",
			timeout=timeout,
			enqueue_after_commit=False,
			job_id=job_id,
			deduplicate=True,
			**kwargs,
		)
	except Exception as error:
		# Authority/outbox are already durable. Leave them for the bounded cron
		# scan; neither queue nor audit failure may turn this into a fake rollback.
		try:
			with service_audit_scope(hint.correlation_id):
				emit_audit_event(
					"budget_release_dispatch_deferred",
					logger_name="medops.budget",
					kind=hint.kind,
					cause_type=type(error).__name__,
				)
		except Exception:
			return


def dispatch_authorized_release(release, message, revision, *, correlation_id):
	hint = _DispatchHint(
		"authorize",
		normalize_reference(message, label="message"),
		correlation_id,
		release=normalize_reference(release, label="release"),
		revision=normalize_positive_integer(revision, label="revision"),
	)
	frappe.db.after_commit.add(partial(_enqueue_committed, hint))


def dispatch_claim(message):
	hint = _DispatchHint(
		"claim", normalize_reference(message, label="message"), ensure_audit_context().correlation_id
	)
	frappe.db.after_commit.add(partial(_enqueue_committed, hint))


def dispatch_execution(message, receipt):
	hint = _DispatchHint(
		"execute",
		normalize_reference(message, label="message"),
		ensure_audit_context().correlation_id,
		receipt=normalize_reference(receipt, label="receipt"),
	)
	frappe.db.after_commit.add(partial(_enqueue_committed, hint))
