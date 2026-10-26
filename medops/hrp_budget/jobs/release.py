from __future__ import annotations

from functools import partial

import frappe
from frappe.utils import get_datetime, now_datetime

from medops.common.budget_ledger import RELEASE_EVENT_TYPE
from medops.common.budget_release_control import ReleaseExecutionLease, release_execution
from medops.common.domain_service import fingerprint_json
from medops.common.error_catalog import IoneApplicationError
from medops.common.integration_outbox import OUTBOX, OUTBOX_DEFINITION
from medops.common.organization import normalize_positive_integer, normalize_reference
from medops.hrp_budget.services.release_access import get_release
from medops.hrp_budget.services.release_confirmation import require_financial_actor
from medops.hrp_budget.services.release_execution import (
	EXECUTION_TIMEOUT,
	LEASE_SECONDS,
	RELEASE_WORKER,
	ExecuteBudgetRelease,
	ExecuteRelease,
	message_authority,
	refresh_financial_authority,
)
from medops.hrp_budget.services.release_recovery import recover_release_message
from medops.hrp_integration.services.outbox import ClaimIntegrationOutbox, FailIntegrationOutbox
from medops.services.audit_context import emit_audit_event, ensure_audit_context, service_audit_scope
from medops.services.errors import raise_ione_error, require_roles
from medops.services.idempotency import read_owned_idempotency_receipt
from medops.services.transactional_message import (
	ClaimOutboxMessage,
	FailMessage,
	read_message_document,
	select_outbox_work,
)

CLAIM_JOB = "medops.hrp_budget.jobs.release.claim_authorized_release"
EXECUTE_JOB = "medops.hrp_budget.jobs.release.execute_authorized_release"
FAILURE_MESSAGE = "预算执行未完成，请核验授权及配置后重试。"


def claim_receipt_key(message, attempt):
	message = normalize_reference(message, label="message")
	attempt = normalize_positive_integer(attempt, label="attempt")
	if attempt > OUTBOX_DEFINITION.max_attempts:
		raise ValueError("claim attempt exceeds reviewed limit")
	return "budget-release-claim:" + fingerprint_json([message, attempt])


def prepare_owned_job(method, arguments):
	job = getattr(frappe.local, "job", None)
	if (
		getattr(frappe.local, "request", None)
		or not job
		or job.method != method
		or dict(job.kwargs) != arguments
		or frappe.db.transaction_writes
		or frappe.db.auto_commit_on_many_writes
	):
		raise_ione_error("OPERATION_NOT_ALLOWED")
	# Only this owned outer job boundary may discard framework setup reads.
	# Unknown before_job writes fail closed; no business service restarts a TX.
	frappe.db.rollback()
	frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
	frappe.db.begin()
	frappe.set_user("Administrator")


def claim_command(message):
	return ClaimOutboxMessage(message_name=message, worker=RELEASE_WORKER, lease_seconds=LEASE_SECONDS)


def dispatch_execution(message, receipt):
	frappe.enqueue(
		EXECUTE_JOB,
		queue="long",
		timeout=EXECUTION_TIMEOUT,
		enqueue_after_commit=True,
		job_id="medops-release-execute-" + fingerprint_json([message, receipt]),
		deduplicate=True,
		message=message,
		receipt=receipt,
	)


def claim_authorized_release(message):
	message = normalize_reference(message, label="message")
	prepare_owned_job(CLAIM_JOB, dict(message=message))
	try:
		doc, parent, recovered = recover_release_message(message)
		if recovered is not None:
			return recovered
		if parent is None:
			raise_ione_error("CONFIGURATION_INVALID")
		if parent.status != "Authorized" or doc.status in {"Delivered", "Dead Letter"}:
			return dict(message=message, status="Skipped")
		if doc.status == "Processing" and get_datetime(doc.lease_expires_at) > now_datetime():
			receipt = claim_receipt_key(message, int(doc.attempt_count))
		else:
			receipt = claim_receipt_key(message, int(doc.attempt_count) + 1)
			ClaimIntegrationOutbox().execute(claim_command(message), idempotency_key=receipt)
		dispatch_execution(message, receipt)
		return dict(message=message, status="Dispatched")
	except Exception as error:
		frappe.db.rollback()
		emit_audit_event(
			"budget_release_claim_deferred", logger_name="medops.budget", cause_type=type(error).__name__
		)
		return dict(message=message, status="Deferred")


def record_committed_post(release, message, revision, line_count, correlation_id):
	"""Callback captures identifiers/counts only, never a document or secret lease."""
	try:
		row = frappe.db.get_value(
			"HRP Budget Release",
			release,
			["status", "docstatus", "revision", "ledger_count", "outbox_message"],
			as_dict=True,
		)
		if (
			not row
			or row.status != "Posted"
			or int(row.docstatus) != 1
			or int(row.revision) != revision
			or int(row.ledger_count) != line_count
			or row.outbox_message != message
			or frappe.db.get_value(OUTBOX, message, "status") != "Delivered"
		):
			return
		with service_audit_scope(correlation_id):
			emit_audit_event(
				"budget_release_posted", logger_name="medops.budget", line_count=line_count, revision=revision
			)
	except Exception as error:
		emit_audit_event(
			"budget_release_commit_audit_deferred",
			logger_name="medops.budget",
			cause_type=type(error).__name__,
		)


def load_claim_receipt(message, receipt):
	service = ClaimIntegrationOutbox()
	result = read_owned_idempotency_receipt(
		service.definition, receipt, service.request_payload(claim_command(message))
	)
	if (
		result["message"] != message
		or result["status"] != "Processing"
		or receipt
		!= claim_receipt_key(message, normalize_positive_integer(result["attempt_count"], label="attempt"))
	):
		raise_ione_error("CONFLICT")
	return result


def record_failed_execution(message, receipt, error):
	frappe.db.rollback()
	frappe.set_user("Administrator")
	code = error.code if isinstance(error, IoneApplicationError) else "MEDOPS-RELEASE-EXECUTION-FAILED"
	try:
		claim = load_claim_receipt(message, receipt)
		FailIntegrationOutbox().execute(
			FailMessage(
				message_name=message,
				processing_token=str(claim["processing_token"]),
				error_code=code,
				error_message=FAILURE_MESSAGE,
				retry_after_seconds=60,
			),
			idempotency_key="release-failure:" + fingerprint_json([message, receipt]),
		)
	except Exception:
		# A replaced/completed lease belongs to another attempt; never change it.
		frappe.db.rollback()
	emit_audit_event(
		"budget_release_execution_failed",
		logger_name="medops.budget",
		error_code=code,
		cause_type=type(error).__name__,
	)
	return dict(message=message, status="Failed", error_code=code)


def execute_authorized_release(message, receipt):
	message = normalize_reference(message, label="message")
	receipt = normalize_reference(receipt, label="receipt")
	prepare_owned_job(EXECUTE_JOB, dict(message=message, receipt=receipt))
	try:
		claim = load_claim_receipt(message, receipt)
		outbox = read_message_document(OUTBOX_DEFINITION, message)
		authority, parent = message_authority(outbox)
		refresh_financial_authority(authority.actor)
		if parent.status == "Posted" and outbox.status == "Delivered":
			doc = get_release(authority.release)
			require_financial_actor(doc)
			return dict(message=message, status="Posted", revision=int(doc.revision))
		lease = ReleaseExecutionLease(
			authority=authority,
			message=message,
			attempt=normalize_positive_integer(claim["attempt_count"], label="attempt"),
			expires_at=get_datetime(claim["lease_expires_at"]),
			processing_token=str(claim["processing_token"]),
		)
		with release_execution(lease):
			result = ExecuteBudgetRelease().execute(
				ExecuteRelease(message), idempotency_key="release-execution:" + authority.event_id
			)
		if not result.idempotency_replayed:
			frappe.db.after_commit.add(
				partial(
					record_committed_post,
					authority.release,
					message,
					int(str(result.result["revision"])),
					int(parent.line_count),
					ensure_audit_context().correlation_id,
				)
			)
		return dict(message=message, status="Posted", revision=result.result["revision"])
	except Exception as error:
		return record_failed_execution(message, receipt, error)


def dispatch_claim(message):
	frappe.enqueue(
		CLAIM_JOB,
		queue="long",
		timeout=300,
		enqueue_after_commit=True,
		job_id="medops-release-claim-" + message,
		deduplicate=True,
		message=message,
	)


def due_release_messages(now):
	# Separate quotas keep live leases from starving new or expired work.
	return select_outbox_work(OUTBOX_DEFINITION, RELEASE_EVENT_TYPE, now)


def dispatch_due_releases():
	require_roles({"System Manager", "HRP System Manager"})
	if getattr(frappe.local, "request", None):
		raise_ione_error("OPERATION_NOT_ALLOWED")
	# Infrastructure scan contains no organization facts and grants no authority.
	now = now_datetime()
	rows = due_release_messages(now)
	for name in rows:
		dispatch_claim(name)
	return dict(dispatched=len(rows))
