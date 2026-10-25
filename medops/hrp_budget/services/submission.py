from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import now_datetime, today

from medops.common.budget_approval import (
	SUBMISSION,
	DepartmentSubmission,
	freeze_department_facts,
	submission_sealing,
)
from medops.common.budget_planning import (
	FUND,
	MAX_LINES,
	PLAN,
	VALUE,
	plan_definition,
	planning_digest,
	version_definition,
)
from medops.common.domain_service import DomainServiceDefinition
from medops.common.organization import normalize_reference
from medops.hrp_budget.services.planning import (
	bare_result,
	execution_payload,
	lock_hospital,
	locked_plan_names,
)
from medops.hrp_budget.services.planning_access import (
	READ_ROLES,
	WRITE_ROLES,
	get_plan,
	get_version,
	native_source,
)
from medops.hrp_budget.services.submission_access import (
	get_submission,
	submission_permission,
	submission_query_scope,
)
from medops.hrp_workflow_authorization.services.business_rules import enforce_business_rules
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.data_access import require_scope
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_authenticated_user, require_roles


def require_submission_scope(plan):
	for action in ("create", "submit"):
		require_scope(
			SUBMISSION,
			company=plan.company,
			hospital=plan.hospital,
			organization_unit=plan.organization_unit,
			action=action,
		)


def verify_frozen_integrity(version):
	"""Verify the sealed version without disclosing other departments' plans.

	The caller already holds the hospital/version fence and checks the selected
	plan's full live permission. Other plans are read solely for this integrity
	calculation, never returned, logged, or used to grant source access.
	"""
	if version.docstatus != 1 or version.status != "Frozen":
		raise_ione_error("INVALID_STATE_TRANSITION")
	refs = []
	count = 0
	for name in locked_plan_names(version.name):
		plan = frappe.get_doc(PLAN, name, for_update=True)
		count += len(plan.lines)
		if count > MAX_LINES * 10:
			raise_ione_error("CONFIGURATION_INVALID")
		refs.append(
			{
				"name": plan.name,
				"included": plan.status != "Withdrawn",
				"digest": planning_digest(plan_definition(plan.as_public_dict())),
			}
		)
	digest = planning_digest({"version": version_definition(version.as_public_dict()), "plans": refs})
	if digest != version.snapshot_digest:
		raise_ione_error("CONFLICT")


class SealDepartmentSubmission(DomainService[DepartmentSubmission]):
	definition = DomainServiceDefinition(
		name="hrp_budget.submission.seal_department", version=1, kind="command", required_roles=WRITE_ROLES
	)

	def request_payload(self, command):
		return {"source_plan": command.source_plan, "expected_revision": command.expected_revision}

	def validate(self, command):
		require_submission_scope(get_plan(command.source_plan))

	def authorize_result(self, result):
		get_submission(str(result["name"]))

	def perform(self, command):
		initial = get_plan(command.source_plan)
		lock_hospital(initial.hospital)
		version = get_version(initial.budget_version, lock=True)
		verify_frozen_integrity(version)
		plan = get_plan(command.source_plan, lock=True)
		require_submission_scope(plan)
		if int(plan.revision) != command.expected_revision:
			raise_ione_error("CONFLICT")
		try:
			snapshot = freeze_department_facts(
				plan.as_public_dict(),
				version.as_public_dict(),
				version_digest=version.snapshot_digest,
				applicant=frappe.session.user,
				posting_date=today(),
			)
		except (ValueError, TypeError, KeyError) as exc:
			raise_ione_error("INVALID_STATE_TRANSITION", cause=exc)
		if frappe.db.get_value(SUBMISSION, snapshot.identity_key, "name", for_update=True):
			raise_ione_error("CONFLICT")
		enforce_business_rules(plan, "submit", required=False)
		with submission_sealing():
			doc = frappe.get_doc(
				{
					"doctype": SUBMISSION,
					**snapshot.as_dict(),
					"status": "Sealed",
					"revision": 1,
					"sealed_by": frappe.session.user,
					"sealed_at": now_datetime(),
				}
			).insert(ignore_permissions=True)
			doc.flags.ignore_permissions = True
			doc.submit()
		emit_audit_event(
			"budget_department_sealed",
			logger_name="medops.budget",
			line_count=len(snapshot.lines),
			revision=1,
		)
		return bare_result(doc)


def seal_department_submission(command: DepartmentSubmission, *, idempotency_key=None, correlation_id=None):
	return execution_payload(
		SealDepartmentSubmission().execute(
			command, idempotency_key=idempotency_key, correlation_id=correlation_id
		)
	)


def read_submission(name: str):
	with service_audit_scope():
		require_authenticated_user()
		require_roles(READ_ROLES)
		doc = get_submission(name)
		result = doc.as_public_dict()
		labels: dict[str, dict[str, str]] = {
			"organization_unit": {},
			"budget_subject": {},
			"funding_source": {},
		}
		for field, doctype in (
			("organization_unit", "HRP Organization Unit"),
			("budget_subject", VALUE),
			("funding_source", FUND),
		):
			for reference in sorted({row.get(field) for row in doc.lines}):
				source = native_source(doctype, reference, {"display_name"})
				if source is not None:
					labels[field][reference] = source.display_name
		result["display_labels"] = labels
		return result


def list_submissions(filters: dict[str, Any], *, offset=0, limit=50):
	with service_audit_scope():
		require_authenticated_user()
		require_roles(READ_ROLES)
		if set(filters) - {
			"name",
			"company",
			"hospital",
			"organization_unit",
			"source_plan",
			"source_version",
			"fiscal_year",
		}:
			raise_ione_error("INVALID_REQUEST")
		if not 0 <= offset <= 5000 or not 1 <= limit <= 100:
			raise_ione_error("INVALID_REQUEST")
		try:
			filters = {key: normalize_reference(value, label=key) for key, value in filters.items()}
		except ValueError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		with submission_query_scope():
			names = frappe.get_list(
				SUBMISSION, filters=filters, pluck="name", limit=5001, order_by="creation desc, name asc"
			)
		if len(names) > 5000:
			raise_ione_error("INVALID_REQUEST")
		items = []
		for name in names:
			doc = frappe.get_doc(SUBMISSION, name)
			if frappe.has_permission(SUBMISSION, "read", doc=doc) and submission_permission(
				doc, ptype="read"
			):
				item = doc.as_public_dict()
				item.pop("lines")
				items.append(item)
		return {
			"items": items[offset : offset + limit],
			"limit": limit,
			"next_offset": offset + limit if len(items) > offset + limit else None,
		}
