from __future__ import annotations

from typing import Any

import frappe

from medops.api.v1.finance.planning import filters_for, pagination, request_guard
from medops.common.budget_workflow import build_case_request, build_human_confirmation, build_review_intent
from medops.common.organization import normalize_reference
from medops.hrp_budget.services import approval
from medops.hrp_budget.services.planning_access import READ_ROLES, WRITE_ROLES


@frappe.whitelist(allow_guest=True, methods=["POST"])
def request_case(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, WRITE_ROLES, correlation_id):
		approval.require_web_session()
		return approval.request_approval(build_case_request(payload), correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def prepare(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, approval.ACTION_ROLES, correlation_id):
		approval.require_web_session()
		return approval.prepare_decision(build_review_intent(payload), correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def confirm(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, approval.ACTION_ROLES, correlation_id):
		approval.require_web_session()
		return approval.confirm_decision(build_human_confirmation(payload), correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_record(name: Any = None, **extra):
	with request_guard(extra, READ_ROLES):
		return approval.read_case(normalize_reference(name, label="name"))


@frappe.whitelist(allow_guest=True, methods=["GET"])
def list_records(filters: Any = None, offset: Any = 0, limit: Any = 50, **extra):
	with request_guard(extra, READ_ROLES):
		start, length = pagination(offset, limit)
		return approval.list_cases(
			filters_for(
				filters, {"name", "company", "hospital", "organization_unit", "submission", "status"}
			),
			offset=start,
			limit=length,
		)
