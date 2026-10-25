from __future__ import annotations

from typing import Any

import frappe

from medops.common.core_reports import report_filters
from medops.hrp_foundation.services.reference_links import register_references, visible_references
from medops.services.audit_context import service_audit_scope
from medops.services.core_reports import execute_core_report
from medops.services.core_workbench import workbench
from medops.services.errors import raise_ione_error, require_authenticated_user


def authenticate(extra: dict[str, object]) -> None:
	require_authenticated_user()
	if set(extra) - {"cmd"}:
		raise_ione_error("INVALID_REQUEST")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def register_population(
	scope: Any = None, entries: Any = None, correlation_id: str | None = None, **extra: object
) -> dict[str, Any]:
	with service_audit_scope(correlation_id):
		authenticate(extra)
		return register_references(scope, entries, idempotency_key=None, correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def overview(filters: Any = None, correlation_id: str | None = None, **extra: object) -> dict[str, Any]:
	with service_audit_scope(correlation_id):
		authenticate(extra)
		return workbench(filters)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def report(
	code: str | None = None, filters: Any = None, correlation_id: str | None = None, **extra: object
) -> dict[str, Any]:
	with service_audit_scope(correlation_id):
		authenticate(extra)
		if code is None:
			raise_ione_error("INVALID_REQUEST")
		columns, rows = execute_core_report(code, filters, export=False)
		return {"columns": columns, "rows": rows}


@frappe.whitelist(allow_guest=True, methods=["GET"])
def population(filters: Any = None, correlation_id: str | None = None, **extra: object) -> dict[str, Any]:
	with service_audit_scope(correlation_id):
		authenticate(extra)
		try:
			context = report_filters(filters)
			if set(context) - {"company", "hospital", "organization_unit", "master_data_domain"}:
				raise ValueError("unknown registration filter")
		except (ValueError, TypeError) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return {"items": [doc.as_public_dict() for doc in visible_references(context)]}
