from __future__ import annotations

from dataclasses import asdict
from itertools import groupby
from typing import TypeVar

import frappe
from frappe.utils import now_datetime

from medops.common.budget_ledger import (
	LEDGER,
	RELEASE,
	BudgetReleaseFacts,
	ReleaseRequest,
	ReverseRequest,
	ReviseReleaseRequest,
	release_facts,
	reversal_facts,
)
from medops.common.budget_planning import PlanLine
from medops.common.budget_release_control import expected_release_record, release_write
from medops.common.domain_service import DomainServiceDefinition
from medops.hrp_budget.services.access_graph import read_graph
from medops.hrp_budget.services.consolidation_access import aggregation_root
from medops.hrp_budget.services.planning import (
	canonical_lines,
	execution_payload,
	native_document,
	unit_context,
)
from medops.hrp_budget.services.planning_access import MANAGER_ROLES, get_version
from medops.hrp_budget.services.release_access import get_release
from medops.hrp_budget.services.release_sources import approved_release_source, stored_release_facts
from medops.hrp_workflow_authorization.services.business_rules import enforce_business_rules
from medops.services.audit_context import emit_audit_event
from medops.services.data_access import require_scope
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error

ReleaseCommand = TypeVar("ReleaseCommand", ReleaseRequest, ReverseRequest, ReviseReleaseRequest)


def require_release_actor():
	state = frappe.db.get_value("User", frappe.session.user, ["enabled", "user_type"], as_dict=True)
	if (
		not state
		or not state.enabled
		or state.user_type != "System User"
		or not MANAGER_ROLES.intersection(frappe.get_roles())
	):
		raise_ione_error("PERMISSION_DENIED")


def require_release_scope(facts, *actions):
	for action in actions:
		require_scope(
			RELEASE,
			company=facts.snapshot.context.company,
			hospital=facts.snapshot.context.hospital,
			organization_unit=facts.snapshot.organization_unit,
			action=action,
		)


def require_operational_date(doc, posting_date):
	# Hospital and organization dates are optional; budget-master dates are not.
	if (
		not doc.enabled
		or (doc.valid_from and str(doc.valid_from) > posting_date)
		or (doc.valid_to and str(doc.valid_to) < posting_date)
	):
		raise_ione_error("INVALID_STATE_TRANSITION")


@read_graph
def require_operational_release(facts):
	context = facts.snapshot.context
	year = native_document(
		"Fiscal Year",
		context.fiscal_year,
		{"year_start_date", "year_end_date", "disabled", "companies.company"},
	)
	if year.disabled or not str(year.year_start_date) <= facts.posting_date <= str(year.year_end_date):
		raise_ione_error("INVALID_STATE_TRANSITION")
	if year.companies and context.company not in {row.company for row in year.companies}:
		raise_ione_error("CONFLICT")
	hospital = native_document(
		"HRP Hospital", context.hospital, {"company", "enabled", "valid_from", "valid_to"}
	)
	if hospital.company != context.company:
		raise_ione_error("CONFLICT")
	require_operational_date(hospital, facts.posting_date)
	# Reversal retains the exact original grain, including now-disabled masters.
	# Both operations still require current read/scope grants and an open fiscal year.
	if facts.operation == "Reverse":
		return
	aggregation_root(
		context, facts.snapshot.organization_unit, operational=True, posting_date=facts.posting_date
	)
	for _, original_rows in groupby(
		sorted(facts.snapshot.lines, key=lambda row: row.origin_plan), key=lambda row: row.origin_plan
	):
		rows = tuple(original_rows)
		version = get_version(rows[0].origin_version)
		unit, center = unit_context(version, rows[0].organization_unit)
		require_operational_date(unit, facts.posting_date)
		if center.name != rows[0].cost_center or any(row.cost_center != center.name for row in rows):
			raise_ione_error("CONFLICT")
		# Use the frozen version's precision; never recapture the current default.
		canonical_lines(
			tuple(
				PlanLine(**{key: getattr(row, key) for key in PlanLine.__dataclass_fields__}) for row in rows
			),
			version,
		)


def release_values(facts, *, revision=1):
	return dict(
		**facts.facts(),
		identity_key=facts.identity_key,
		intent_digest=facts.intent_digest,
		subject=(
			("预算下达" if facts.operation == "Release" else "预算冲销")
			+ "｜"
			+ facts.snapshot.context.fiscal_year
		)[:140],
		status="Prepared",
		revision=revision,
		prepared_at=now_datetime(),
	)


def release_result(doc):
	return dict(
		doctype=RELEASE, name=doc.name, revision=int(doc.revision), operation=doc.operation, status=doc.status
	)


def lock_release_source(name):
	initial = get_release(name)
	# Every financial writer takes final source -> voucher, in that order.
	approved_release_source(initial.source_approval, lock=True)
	if initial.operation == "Reverse":
		get_release(initial.reversal_of, lock=True)
	doc = get_release(name, lock=True)
	if initial.source_approval != doc.source_approval or initial.reversal_of != doc.reversal_of:
		raise_ione_error("CONFLICT")
	return doc


def assert_reversible(original):
	if original.operation != "Release" or original.status != "Posted" or original.docstatus != 1:
		raise_ione_error("INVALID_STATE_TRANSITION")
	facts = stored_release_facts(original)
	plans = sorted({line.origin_plan for line in facts.snapshot.lines})
	# New execution types must extend reversal eligibility in the same release.
	if frappe.db.get_value(
		LEDGER, {"origin_plan": ("in", plans), "entry_type": ("!=", "Allocation")}, "name"
	):
		raise_ione_error("INVALID_STATE_TRANSITION")
	return facts


def insert_prepared_release(facts):
	name = frappe.db.get_value(RELEASE, facts.identity_key, "name", for_update=True, wait=False)
	if name:
		existing = get_release(name)
		if existing.intent_digest != facts.intent_digest:
			raise_ione_error("CONFLICT")
		return existing
	values = release_values(facts)
	with release_write("prepare", facts, 0), expected_release_record(RELEASE, values):
		doc = frappe.get_doc(dict(doctype=RELEASE, name=facts.identity_key, **values))
		doc.set("__islocal", 1)
		doc.insert(ignore_permissions=True)
		# Rule evidence has a real Dynamic Link. Both the prepared record and its
		# guard evidence are rolled back by the owned service savepoint on denial.
		enforce_business_rules(doc, "create")
	return doc


class ReleasePreparationService(DomainService[ReleaseCommand]):
	definition = DomainServiceDefinition(
		name="hrp_budget.release.create", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		require_release_actor()

	def request_payload(self, command):
		return asdict(command)

	def facts_for(self, command: ReleaseCommand, *, lock=False) -> BudgetReleaseFacts:
		raise NotImplementedError

	def validate(self, command):
		facts = self.facts_for(command)
		require_release_scope(facts, "create", "write", "submit")
		require_operational_release(facts)

	def authorize_result(self, result):
		get_release(str(result["name"]))

	def perform(self, command):
		facts = self.facts_for(command, lock=True)
		require_release_scope(facts, "create", "write", "submit")
		require_operational_release(facts)
		doc = insert_prepared_release(facts)
		emit_audit_event(
			"budget_release_prepared", logger_name="medops.budget", line_count=len(facts.snapshot.lines)
		)
		return release_result(doc)


class CreateBudgetRelease(ReleasePreparationService[ReleaseRequest]):
	def facts_for(self, command: ReleaseRequest, *, lock=False) -> BudgetReleaseFacts:
		case, _, snapshot, proof = approved_release_source(command.approval_case, lock=lock)
		if int(case.revision) != command.expected_approval_revision:
			raise_ione_error("CONFLICT")
		return release_facts(
			snapshot,
			proof,
			posting_date=command.posting_date,
			reason=command.reason,
			prepared_by=frappe.session.user,
		)


class CreateBudgetReversal(ReleasePreparationService[ReverseRequest]):
	definition = DomainServiceDefinition(
		name="hrp_budget.release.reverse", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def facts_for(self, command: ReverseRequest, *, lock=False):
		doc = lock_release_source(command.release) if lock else get_release(command.release)
		if int(doc.revision) != command.expected_revision:
			raise_ione_error("CONFLICT")
		return reversal_facts(
			assert_reversible(doc),
			posting_date=command.posting_date,
			reason=command.reason,
			prepared_by=frappe.session.user,
		)


class ReviseBudgetRelease(ReleasePreparationService[ReviseReleaseRequest]):
	definition = DomainServiceDefinition(
		name="hrp_budget.release.revise", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def facts_for(self, command: ReviseReleaseRequest, *, lock=False):
		doc = lock_release_source(command.release) if lock else get_release(command.release)
		if doc.status != "Prepared" or doc.docstatus or int(doc.revision) != command.expected_revision:
			raise_ione_error("CONFLICT")
		original = stored_release_facts(doc)
		if original.operation == "Release":
			return release_facts(
				original.snapshot,
				original.approval,
				posting_date=command.posting_date,
				reason=command.reason,
				prepared_by=frappe.session.user,
			)
		return reversal_facts(
			assert_reversible(get_release(original.reversal_of)),
			posting_date=command.posting_date,
			reason=command.reason,
			prepared_by=frappe.session.user,
		)

	def validate(self, command):
		facts = self.facts_for(command)
		require_release_scope(facts, "write", "submit")
		require_operational_release(facts)

	def perform(self, command):
		facts = self.facts_for(command, lock=True)
		doc = get_release(command.release, lock=True)
		require_release_scope(facts, "write", "submit")
		require_operational_release(facts)
		values = release_values(facts, revision=command.expected_revision + 1)
		with (
			release_write("revise", facts, command.expected_revision),
			expected_release_record(RELEASE, values),
		):
			doc.update(values)
			doc.save(ignore_permissions=True)
			enforce_business_rules(doc, "amend")
		return release_result(doc)


def create_budget_release(command, *, idempotency_key=None, correlation_id=None):
	return execution_payload(
		CreateBudgetRelease().execute(command, idempotency_key=idempotency_key, correlation_id=correlation_id)
	)


def create_budget_reversal(command, *, idempotency_key=None, correlation_id=None):
	return execution_payload(
		CreateBudgetReversal().execute(
			command, idempotency_key=idempotency_key, correlation_id=correlation_id
		)
	)


def revise_budget_release(command, *, idempotency_key=None, correlation_id=None):
	return execution_payload(
		ReviseBudgetRelease().execute(command, idempotency_key=idempotency_key, correlation_id=correlation_id)
	)
