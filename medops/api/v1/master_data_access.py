from __future__ import annotations

from datetime import date
from typing import Any

import frappe

from medops.common.organization import (
	normalize_nonnegative_integer,
	normalize_optional_date,
	normalize_optional_text,
	validate_date_range,
)
from medops.hrp_master_data.services.access import get_scoped_documents
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.errors import raise_ione_error, require_authenticated_user

ENTITIES = {
	"requests": "HRP Master Data Request",
	"mappings": "HRP External Code Mapping",
	"rules": "HRP Data Quality Rule",
	"issues": "HRP Data Quality Issue",
}
STATES = {
	"requests": {"Draft", "Pending Review", "Approved", "Rejected"},
	"issues": {"Open", "Resolved"},
	"rules": {"Enabled", "Disabled"},
	"mappings": {"Enabled", "Disabled"},
}


@frappe.whitelist(allow_guest=True, methods=["GET"])
def list_records(
	entity: str | None = None,
	company: str | None = None,
	hospital: str | None = None,
	organization_unit: str | None = None,
	status: str | None = None,
	from_date: str | None = None,
	to_date: str | None = None,
	offset: int | str = 0,
	limit: int | str = 20,
	correlation_id: str | None = None,
	**extra: object,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		if set(extra) - {"cmd"}:
			raise_ione_error("INVALID_REQUEST")
		try:
			if entity not in ENTITIES:
				raise ValueError("unknown entity")
			start = normalize_nonnegative_integer(offset, label="offset")
			length = normalize_nonnegative_integer(limit, label="limit")
			if start > 5000 or not 1 <= length <= 50:
				raise ValueError("pagination exceeds limits")
			filters: dict[str, Any] = {}
			for key, value in (
				("company", company),
				("hospital", hospital),
				("organization_unit", organization_unit),
			):
				if normalized := normalize_optional_text(value, label=key, maximum=140):
					filters[key] = normalized
			if "organization_unit" in filters and "hospital" not in filters:
				raise ValueError("organization filter requires hospital")
			if "hospital" in filters and "company" not in filters:
				raise ValueError("hospital filter requires company")
			state = normalize_optional_text(status, label="status", maximum=30)
			if state:
				if state not in STATES[entity]:
					raise ValueError("unknown status")
				field = {"requests": "request_status", "issues": "issue_status"}.get(entity, "enabled")
				filters[field] = int(state == "Enabled") if field == "enabled" else state
			first = normalize_optional_date(from_date, label="from_date")
			last = normalize_optional_date(to_date, label="to_date")
			if bool(first) != bool(last):
				raise ValueError("both date bounds are required")
			if first and last:
				validate_date_range(first, last)
				if (date.fromisoformat(last) - date.fromisoformat(first)).days > 365:
					raise ValueError("date range exceeds one year")
				filters["modified"] = ("between", [first + " 00:00:00", last + " 23:59:59.999999"])
		except (ValueError, TypeError) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		docs = get_scoped_documents(ENTITIES[entity], filters=filters)
		items = []
		for doc in docs[start : start + length]:
			items.append(
				{
					"name": doc.name,
					"doctype": doc.doctype,
					"title": doc.get("subject")
					or doc.get("display_name")
					or doc.get("target_name")
					or doc.get("internal_name")
					or doc.name,
					"company": doc.company,
					"hospital": doc.hospital,
					"organization_unit": doc.organization_unit or None,
					"status": doc.get("request_status")
					or doc.get("issue_status")
					or ("Enabled" if doc.get("enabled") else "Disabled"),
					"modified": str(doc.modified),
				}
			)
		emit_audit_event(
			"scoped_master_data_list_read",
			logger_name="medops.master_data",
			entity=entity,
			returned=len(items),
		)
		return {
			"items": items,
			"next_offset": start + length if len(docs) > start + length else None,
			"limit": length,
		}
