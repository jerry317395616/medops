from __future__ import annotations

import json

import frappe
from frappe.utils import now_datetime, today

from medops.common.budget_approval import SUBMISSION, submission_sealing
from medops.common.budget_consolidation import ConsolidationCommand, consolidate_departments
from medops.common.domain_service import DomainServiceDefinition
from medops.hrp_budget.services.consolidation_access import (
	aggregation_root,
	approved_department,
	assert_leaf_ancestry,
)
from medops.hrp_budget.services.planning import bare_result, execution_payload, lock_hospital
from medops.hrp_budget.services.planning_access import MANAGER_ROLES, READ_ROLES
from medops.hrp_budget.services.submission_access import get_submission
from medops.hrp_workflow_authorization.services.business_rules import enforce_business_rules
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.data_access import require_scope
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_authenticated_user, require_roles


def selected_departments(command, *, lock=False):
	items = []
	for selection in command.sources:
		ref, snapshot = approved_department(selection.submission, selection.approval_case, lock=lock)
		if (
			selection.expected_revision != 1
			or ref.approval_revision != selection.expected_approval_revision
			or ref.snapshot_digest != selection.snapshot_digest
		):
			raise_ione_error("CONFLICT")
		items.append((ref, snapshot))
	return tuple(items)


def check_creation_scope(context, unit):
	for action in ("create", "submit"):
		require_scope(
			SUBMISSION,
			company=context.company,
			hospital=context.hospital,
			organization_unit=unit,
			action=action,
		)


class SealConsolidation(DomainService[ConsolidationCommand]):
	definition = DomainServiceDefinition(
		name="hrp_budget.submission.seal_consolidated",
		version=1,
		kind="command",
		required_roles=MANAGER_ROLES,
	)

	def request_payload(self, command):
		return command.request_payload()

	def validate(self, command):
		departments = selected_departments(command)
		context = departments[0][1].context
		aggregation_root(context, command.organization_unit, operational=True, posting_date=today())
		check_creation_scope(context, command.organization_unit)

	def authorize_result(self, result):
		get_submission(str(result["name"]))

	def perform(self, command):
		first, _ = approved_department(command.sources[0].submission, command.sources[0].approval_case)
		hospital = frappe.db.get_value(SUBMISSION, first.submission, "hospital")
		lock_hospital(hospital)
		departments = selected_departments(command, lock=True)
		context = departments[0][1].context
		root = aggregation_root(context, command.organization_unit, operational=True, posting_date=today())
		check_creation_scope(context, command.organization_unit)
		assert_leaf_ancestry(context, root, [snapshot for _, snapshot in departments])
		try:
			snapshot = consolidate_departments(
				departments,
				organization_unit=root.name,
				subject=command.subject,
				applicant=frappe.session.user,
				posting_date=today(),
				remarks=command.remarks,
			)
		except (ValueError, TypeError, KeyError) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		if frappe.db.get_value(SUBMISSION, snapshot.identity_key, "name", for_update=True):
			raise_ione_error("CONFLICT")
		values = snapshot.as_dict()
		sources = values.pop("sources")
		with submission_sealing():
			doc = frappe.get_doc(
				dict(
					doctype=SUBMISSION,
					**values,
					sources_snapshot=json.dumps(
						sources, ensure_ascii=False, sort_keys=True, separators=(",", ":")
					),
					status="Sealed",
					revision=1,
					sealed_by=frappe.session.user,
					sealed_at=now_datetime(),
				)
			).insert(ignore_permissions=True)
			enforce_business_rules(doc, "submit", required=False)
			doc.flags.ignore_permissions = True
			doc.submit()
		emit_audit_event(
			"budget_consolidation_sealed",
			logger_name="medops.budget",
			source_count=len(snapshot.sources),
			line_count=len(snapshot.lines),
			revision=1,
		)
		return bare_result(doc)


def seal_consolidation(command, *, idempotency_key=None, correlation_id=None):
	return execution_payload(
		SealConsolidation().execute(command, idempotency_key=idempotency_key, correlation_id=correlation_id)
	)


def eligible_sources(filters, *, offset=0, limit=50):
	from medops.common.budget_workflow import CASE
	from medops.common.error_catalog import IoneApplicationError
	from medops.common.organization import normalize_reference
	from medops.hrp_budget.services.approval_access import case_query_scope

	with service_audit_scope():
		require_authenticated_user()
		require_roles(READ_ROLES)
		if (
			set(filters)
			- {"company", "hospital", "organization_unit", "organization_version", "fiscal_year", "currency"}
			or type(offset) is not int
			or type(limit) is not int
			or not 0 <= offset <= 5000
			or not 1 <= limit <= 100
		):
			raise_ione_error("INVALID_REQUEST")
		filters = {key: normalize_reference(value, label=key) for key, value in filters.items()}
		with case_query_scope():
			rows = frappe.get_list(
				CASE,
				filters={**filters, "status": "Approved", "docstatus": 1},
				fields=["name", "submission"],
				limit=5001,
				order_by="completed_at desc,name asc",
			)
		if len(rows) > 5000:
			raise_ione_error("INVALID_REQUEST")
		items = []
		for row in rows:
			if frappe.db.get_value(SUBMISSION, row.submission, "submission_type") != "Department":
				continue
			try:
				ref, snapshot = approved_department(row.submission, row.name)
			except IoneApplicationError:
				continue
			items.append(
				dict(
					submission=ref.submission,
					expected_revision=1,
					approval_case=ref.approval_case,
					expected_approval_revision=ref.approval_revision,
					snapshot_digest=ref.snapshot_digest,
					**{
						key: getattr(snapshot.context, key)
						for key in (
							"company",
							"hospital",
							"organization_version",
							"fiscal_year",
							"currency",
							"currency_precision",
						)
					},
					organization_unit=snapshot.organization_unit,
					cost_center=snapshot.cost_center,
					subject=snapshot.subject,
					total_amount=snapshot.total_amount,
					line_count=len(snapshot.lines),
				)
			)
		return dict(
			items=items[offset : offset + limit],
			next_offset=offset + limit if len(items) > offset + limit else None,
			limit=limit,
		)
