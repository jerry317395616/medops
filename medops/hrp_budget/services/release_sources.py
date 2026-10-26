from __future__ import annotations

from decimal import localcontext

import frappe

from medops.common.budget_approval import SUBMISSION
from medops.common.budget_consolidation import ConsolidatedSnapshot
from medops.common.budget_ledger import RELEASE, FinalBudgetApproval, release_facts, reversal_facts
from medops.common.budget_workflow import CASE, REVIEW, parse_decision
from medops.hrp_budget.services.access_graph import read_graph
from medops.hrp_budget.services.approval_access import case_permission, review_permission
from medops.hrp_budget.services.approval_evidence import decision_history, progress_for
from medops.hrp_budget.services.release_documents import same_value
from medops.hrp_budget.services.release_evidence import release_authorization
from medops.hrp_budget.services.statement_cache import cached_statement_value
from medops.hrp_budget.services.submission_documents import snapshot_from_document
from medops.services.errors import raise_ione_error


def stored_final_source(case_name, *, lock=False):
	"""Internal complete-source integrity only; never grants source visibility."""
	if lock:
		return _stored_final_source(case_name, lock=True)
	return cached_statement_value(("stored_final_source", case_name), lambda: _stored_final_source(case_name))


def _stored_final_source(case_name, *, lock=False):
	case = frappe.get_doc(CASE, case_name, for_update=lock)
	source = frappe.get_doc(SUBMISSION, case.submission, for_update=lock)
	if (
		case.status != "Approved"
		or case.docstatus != 1
		or int(case.current_step)
		or not case.completed_at
		or source.submission_type != "Consolidated"
		or int(source.schema_version) != 2
		or source.status != "Sealed"
		or source.docstatus != 1
		or case.source_digest != source.snapshot_digest
	):
		raise_ione_error("INVALID_STATE_TRANSITION")
	for key in (
		"company",
		"hospital",
		"organization_version",
		"organization_unit",
		"cost_center",
		"fiscal_year",
		"currency",
		"currency_precision",
		"total_amount",
		"subject",
		"applicant",
	):
		if str(case.get(key)) != str(source.get(key)):
			raise_ione_error("CONFIGURATION_INVALID")
	events = decision_history(case, lock=lock)
	decision = parse_decision(case.decision_snapshot)
	if (
		len(events) != int(case.revision) - 1
		or not events
		or events[-1].name != case.final_review
		or progress_for(case, events).status != "Approved"
		or decision["docname"] != case.name
		or decision["decision_digest"] != case.decision_digest
		or decision["matrix"] != case.policy_matrix
		or decision["policy_digest"] != case.policy_digest
		or decision["policy_version"] != int(case.policy_revision)
		or decision["amount_precision"] != int(case.currency_precision)
		or decision["amount"] != case.total_amount
	):
		raise_ione_error("CONFIGURATION_INVALID")
	with localcontext() as context:
		context.prec = 48
		snapshot = snapshot_from_document(source)
		if not isinstance(snapshot, ConsolidatedSnapshot):
			raise_ione_error("CONFIGURATION_INVALID")
		proof = FinalBudgetApproval.from_payload(
			dict(
				submission=source.name,
				snapshot_digest=source.snapshot_digest,
				approval_case=case.name,
				approval_round=int(case.round_number),
				approval_revision=int(case.revision),
				decision_digest=case.decision_digest,
				final_review=case.final_review,
				event_digest=events[-1].event_digest,
			)
		)
	return case, source, snapshot, proof, events


@read_graph
def approved_release_source(case_name, *, user=None, export=False, lock=False):
	user = user or frappe.session.user
	if lock:
		return _approved_release_source(case_name, user=user, export=export, lock=True)
	return cached_statement_value(
		("approved_release_source", case_name, user, bool(export)),
		lambda: _approved_release_source(case_name, user=user, export=export),
	)


def _approved_release_source(case_name, *, user, export=False, lock=False):
	ptype = "export" if export else "read"
	probe = frappe.get_doc(CASE, case_name)
	if not frappe.has_permission(CASE, ptype, doc=probe, user=user) or not case_permission(
		probe, user=user, ptype=ptype
	):
		raise_ione_error("PERMISSION_DENIED")
	case, source, snapshot, proof, events = stored_final_source(case_name, lock=lock)
	if not frappe.has_permission(CASE, ptype, doc=case, user=user) or not case_permission(
		case, user=user, ptype=ptype
	):
		raise_ione_error("PERMISSION_DENIED")
	for event in events:
		if not frappe.has_permission(REVIEW, ptype, doc=event, user=user) or not review_permission(
			event, user=user, ptype=ptype
		):
			raise_ione_error("PERMISSION_DENIED")
	return case, source, snapshot, proof


def stored_release_facts(doc):
	"""Private batch integrity, including for a reader restricted to an original leaf."""
	_, _, snapshot, proof, _ = stored_final_source(doc.source_approval)
	if doc.operation == "Release":
		facts = release_facts(
			snapshot,
			proof,
			posting_date=str(doc.posting_date),
			reason=doc.reason,
			prepared_by=doc.prepared_by,
		)
	elif doc.operation == "Reverse" and doc.reversal_of:
		original = frappe.get_doc(RELEASE, doc.reversal_of)
		if original.operation != "Release" or original.status != "Posted" or original.docstatus != 1:
			raise_ione_error("INVALID_STATE_TRANSITION")
		facts = reversal_facts(
			stored_release_facts(original),
			posting_date=str(doc.posting_date),
			reason=doc.reason,
			prepared_by=doc.prepared_by,
		)
	else:
		raise_ione_error("CONFIGURATION_INVALID")
	if facts.identity_key != doc.name or facts.intent_digest != doc.intent_digest:
		raise_ione_error("CONFIGURATION_INVALID")
	for key, expected in facts.facts().items():
		field = doc.meta.get_field(key)
		if not field or not same_value(field, doc.get(key), expected):
			raise_ione_error("CONFIGURATION_INVALID")
	if doc.status in {"Authorized", "Posted"}:
		release_authorization(doc)
		if doc.status == "Posted":
			doc.validate_completed_batch()
	elif (
		doc.status != "Prepared"
		or doc.docstatus
		or any(
			doc.get(key)
			for key in (
				"authorization_decision",
				"authorization_revision",
				"authorized_by",
				"authorized_at",
				"outbox_message",
				"posted_at",
				"ledger_count",
				"execution_digest",
			)
		)
	):
		raise_ione_error("CONFIGURATION_INVALID")
	return facts
