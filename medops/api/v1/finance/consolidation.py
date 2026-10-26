from __future__ import annotations

from typing import Any

import frappe

from medops.api.v1.finance.planning import filters_for, pagination, request_guard
from medops.common.budget_consolidation import build_consolidation
from medops.hrp_budget.services.consolidation import eligible_sources, seal_consolidation
from medops.hrp_budget.services.planning_access import MANAGER_ROLES, READ_ROLES


@frappe.whitelist(allow_guest=True, methods=["POST"])
def seal(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, MANAGER_ROLES, correlation_id):
		return seal_consolidation(build_consolidation(payload), correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def eligible(filters: Any = None, offset: Any = 0, limit: Any = 50, **extra):
	with request_guard(extra, READ_ROLES):
		start, length = pagination(offset, limit)
		return eligible_sources(
			filters_for(
				filters,
				{
					"company",
					"hospital",
					"organization_unit",
					"organization_version",
					"fiscal_year",
					"currency",
				},
			),
			offset=start,
			limit=length,
		)
