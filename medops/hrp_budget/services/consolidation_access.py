from __future__ import annotations

import frappe

from medops.common.budget_approval import SUBMISSION, department_snapshot_from_payload
from medops.common.budget_consolidation import (
	ApprovedDepartment,
	consolidate_departments,
	consolidated_snapshot_from_payload,
)
from medops.common.budget_workflow import CASE, REVIEW
from medops.hrp_budget.services.access_graph import access_graph_cache, read_graph
from medops.hrp_budget.services.planning_access import native_source
from medops.hrp_budget.services.statement_cache import read_statement_document
from medops.services.errors import raise_ione_error


@read_graph
def approved_department(submission_name, case_name, *, user=None, export=False, lock=False):
	"""Read a complete approved leaf, never recurse through a consolidated source."""
	from medops.hrp_budget.services.approval_access import case_permission, review_permission
	from medops.hrp_budget.services.approval_evidence import decision_history, progress_for
	from medops.hrp_budget.services.submission_access import submission_permission
	from medops.hrp_budget.services.submission_documents import stored_facts

	user = user or frappe.session.user
	cache = access_graph_cache()
	key = ("approved_department", submission_name, case_name, user, bool(export))
	if not lock and cache is not None and key in cache:
		return cache[key]
	if not frappe.db.exists(SUBMISSION, submission_name) or not frappe.db.exists(CASE, case_name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	doc = read_statement_document(SUBMISSION, submission_name, for_update=lock)
	if (
		doc.submission_type != "Department"
		or int(doc.schema_version) != 1
		or doc.status != "Sealed"
		or int(doc.docstatus) != 1
	):
		raise_ione_error("INVALID_STATE_TRANSITION")
	ptype = "export" if export else "read"
	if not frappe.has_permission(SUBMISSION, ptype, doc=doc, user=user) or not submission_permission(
		doc, user=user, ptype=ptype
	):
		raise_ione_error("PERMISSION_DENIED")
	case = read_statement_document(CASE, case_name, for_update=lock)
	if not frappe.has_permission(CASE, ptype, doc=case, user=user) or not case_permission(
		case, user=user, ptype=ptype
	):
		raise_ione_error("PERMISSION_DENIED")
	if (
		case.submission != doc.name
		or case.source_digest != doc.snapshot_digest
		or case.status != "Approved"
		or int(case.docstatus) != 1
		or int(case.current_step) != 0
		or not case.completed_at
		or not case.final_review
	):
		raise_ione_error("INVALID_STATE_TRANSITION")
	events = decision_history(case, lock=lock)
	if (
		len(events) != int(case.revision) - 1
		or not events
		or events[-1].name != case.final_review
		or progress_for(case, events).status != "Approved"
	):
		raise_ione_error("CONFIGURATION_INVALID")
	for event in events:
		if not frappe.has_permission(REVIEW, ptype, doc=event, user=user) or not review_permission(
			event, user=user, ptype=ptype
		):
			raise_ione_error("PERMISSION_DENIED")
	ref = ApprovedDepartment.from_payload(
		dict(
			submission=doc.name,
			snapshot_digest=doc.snapshot_digest,
			approval_case=case.name,
			approval_round=int(case.round_number),
			approval_revision=int(case.revision),
			approval_decision_digest=case.decision_digest,
			approval_final_review=case.final_review,
			approval_event_digest=events[-1].event_digest,
			origin_plan=doc.source_plan,
			preparer=case.source_applicant,
			applicant=doc.applicant,
			requested_by=case.requested_by,
		)
	)
	result = ref, department_snapshot_from_payload(stored_facts(doc))
	if not lock and cache is not None:
		cache[key] = result
	return result


def aggregation_root(context, unit_name, *, user=None, export=False, operational=False, posting_date=None):
	fields = {
		"company",
		"hospital",
		"organization_version",
		"enabled",
		"valid_from",
		"valid_to",
		"lft",
		"rgt",
	}
	unit = native_source("HRP Organization Unit", unit_name, fields, user=user, export=export)
	if unit is None:
		raise_ione_error("PERMISSION_DENIED")
	if (
		unit.company != context.company
		or unit.hospital != context.hospital
		or unit.organization_version != context.organization_version
	):
		raise_ione_error("CONFLICT")
	if unit.lft is None or unit.rgt is None or int(unit.lft) < 1 or int(unit.rgt) <= int(unit.lft):
		raise_ione_error("CONFIGURATION_INVALID")
	if operational and not isinstance(posting_date, str):
		raise_ione_error("CONFIGURATION_INVALID")
	if (
		operational
		and isinstance(posting_date, str)
		and (
			not unit.enabled
			or (unit.valid_from and str(unit.valid_from) > posting_date)
			or (unit.valid_to and str(unit.valid_to) < posting_date)
		)
	):
		raise_ione_error("INVALID_STATE_TRANSITION")
	return unit


def assert_leaf_ancestry(context, root, snapshots, *, user=None, export=False):
	for name in sorted({snapshot.organization_unit for snapshot in snapshots}):
		unit = native_source(
			"HRP Organization Unit",
			name,
			{"company", "hospital", "organization_version", "lft", "rgt"},
			user=user,
			export=export,
		)
		if unit is None:
			raise_ione_error("PERMISSION_DENIED")
		if (
			unit.company != context.company
			or unit.hospital != context.hospital
			or unit.organization_version != context.organization_version
			or unit.lft is None
			or unit.rgt is None
			or not int(root.lft) <= int(unit.lft) < int(unit.rgt) <= int(root.rgt)
		):
			raise_ione_error("CONFLICT")


@read_graph
def aggregate_sources_valid(doc, *, user, export=False):
	from medops.common.error_catalog import IoneApplicationError
	from medops.hrp_budget.services.submission_documents import stored_facts

	try:
		snapshot = consolidated_snapshot_from_payload(stored_facts(doc))
		root = aggregation_root(snapshot.context, snapshot.organization_unit, user=user, export=export)
		departments = []
		for ref in snapshot.sources:
			actual, leaf = approved_department(ref.submission, ref.approval_case, user=user, export=export)
			if actual != ref:
				return False
			departments.append((actual, leaf))
		assert_leaf_ancestry(
			snapshot.context, root, [leaf for _, leaf in departments], user=user, export=export
		)
		actual = consolidate_departments(
			tuple(departments),
			organization_unit=snapshot.organization_unit,
			subject=snapshot.subject,
			applicant=snapshot.applicant,
			posting_date=snapshot.posting_date,
			remarks=snapshot.remarks,
		)
		return actual == snapshot
	except (IoneApplicationError, ValueError, TypeError, KeyError):
		return False


def assert_unclaimed_approved_sources(case):
	"""A source may appear in proposals, but can receive one consolidated approval."""
	from medops.common.budget_consolidation import source_references
	from medops.common.budget_workflow import approval_mutation

	mutation = approval_mutation()
	if not mutation or mutation.operation != "confirm" or mutation.case != case.name:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	source = read_statement_document(SUBMISSION, case.submission)
	if source.submission_type != "Consolidated":
		return
	names = [ref.origin_plan for ref in source_references(source.sources_snapshot)]
	placeholders = ",".join(["%s"] * len(names))
	rows = frappe.db.sql(
		"""SELECT approval.name FROM `tabHRP Budget Approval` approval
		JOIN `tabHRP Budget Submission` submission ON submission.name=approval.submission
		JOIN `tabHRP Budget Submission Line` line ON line.parent=submission.name
		AND line.parenttype='HRP Budget Submission' AND line.parentfield='lines'
		WHERE approval.status='Approved' AND approval.docstatus=1
		AND submission.submission_type='Consolidated' AND submission.company=%s AND submission.hospital=%s
		AND approval.name<>%s AND line.origin_plan IN ("""
		+ placeholders
		+ ") LIMIT 1 FOR UPDATE",
		(case.company, case.hospital, case.name, *names),
	)
	if rows:
		raise_ione_error("CONFLICT")
