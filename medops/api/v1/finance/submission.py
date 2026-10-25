from __future__ import annotations

from typing import Any

import frappe

from medops.api.v1.finance.planning import filters_for, pagination, request_guard
from medops.common.budget_approval import build_department_submission
from medops.common.organization import normalize_reference
from medops.hrp_budget.services.planning_access import READ_ROLES, WRITE_ROLES
from medops.hrp_budget.services.submission import (
	list_submissions,
	read_submission,
	seal_department_submission,
)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def seal_department(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, WRITE_ROLES, correlation_id):
		return seal_department_submission(build_department_submission(payload), correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_record(name: Any = None, **extra):
	with request_guard(extra, READ_ROLES):
		return read_submission(normalize_reference(name, label="name"))


@frappe.whitelist(allow_guest=True, methods=["GET"])
def list_records(filters: Any = None, offset: Any = 0, limit: Any = 50, **extra):
	with request_guard(extra, READ_ROLES):
		start, length = pagination(offset, limit)
		return list_submissions(
			filters_for(
				filters,
				{
					"name",
					"company",
					"hospital",
					"organization_unit",
					"source_plan",
					"source_version",
					"fiscal_year",
				},
			),
			offset=start,
			limit=length,
		)
