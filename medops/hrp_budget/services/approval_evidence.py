from __future__ import annotations

import frappe

from medops.common.budget_workflow import MAX_CASE_REVIEWS, REVIEW, Vote, parse_decision, replay_votes
from medops.common.domain_service import fingerprint_json
from medops.hrp_budget.services.approval_access import forbidden_actors
from medops.services.errors import raise_ione_error

EVIDENCE_FIELDS = (
	"case",
	"company",
	"hospital",
	"organization_unit",
	"event_kind",
	"action",
	"sequence_no",
	"actor",
	"case_revision",
	"source_digest",
	"policy_digest",
	"decision_digest",
	"sod_digest",
	"reason",
	"recorded_at",
	"expires_at",
	"confirmation_hash",
	"session_digest",
	"confirmation",
	"vote_key",
	"revision_key",
)


def evidence_values(doc):
	result = {key: doc.get(key) for key in EVIDENCE_FIELDS}
	for key in ("recorded_at", "expires_at"):
		result[key] = str(doc.get(key)) if doc.get(key) else None
	for key in ("confirmation", "vote_key", "revision_key"):
		result[key] = result[key] or None
	return result


def validate_evidence(doc):
	try:
		if fingerprint_json(evidence_values(doc)) != doc.event_digest:
			raise ValueError("review evidence differs")
	except (ValueError, TypeError) as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)


def decision_history(case, *, lock=False):
	rows = frappe.db.sql(
		"SELECT name FROM `tabHRP Budget Review` WHERE `case`=%s AND event_kind='Decision' ORDER BY case_revision ASC LIMIT %s"
		+ (" FOR UPDATE" if lock else ""),
		(case.name, MAX_CASE_REVIEWS + 1),
	)
	if len(rows) > MAX_CASE_REVIEWS:
		raise_ione_error("CONFIGURATION_INVALID")
	events = []
	for index, row in enumerate(rows, 1):
		doc = frappe.get_doc(REVIEW, row[0], for_update=lock)
		validate_evidence(doc)
		if int(doc.case_revision) != index or any(
			doc.get(key) != case.get(key)
			for key in (
				"source_digest",
				"policy_digest",
				"decision_digest",
				"company",
				"hospital",
				"organization_unit",
			)
		):
			raise_ione_error("CONFIGURATION_INVALID")
		events.append(doc)
	return events


def progress_for(case, events):
	try:
		return replay_votes(
			parse_decision(case.decision_snapshot),
			tuple(Vote(int(row.sequence_no), row.actor, row.action) for row in events),
			forbidden=forbidden_actors(case),
		)
	except (ValueError, TypeError, KeyError) as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)
