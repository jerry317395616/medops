from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import frappe

from medops.common.budget_planning import (
	MASTER_TYPES,
	PLAN,
	VERSION,
	build_action,
	build_master,
	build_plan,
	build_version,
	object_payload,
)
from medops.common.organization import (
	normalize_nonnegative_integer,
	normalize_reference,
	normalize_required_text,
)
from medops.hrp_budget.services import planning
from medops.hrp_budget.services.planning_access import MANAGER_ROLES, READ_ROLES, WRITE_ROLES, get_plan
from medops.hrp_master_data.services import budget_dimensions as masters
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.errors import raise_ione_error, require_authenticated_user, require_roles


@contextmanager
def request_guard(extra, roles, correlation_id=None):
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		require_roles(roles)
		if set(extra) - {"cmd"}:
			raise_ione_error("INVALID_REQUEST")
		try:
			yield
		except (ValueError, RecursionError) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)


def pagination(offset, limit):
	start = normalize_nonnegative_integer(offset, label="offset")
	length = normalize_nonnegative_integer(limit, label="limit")
	if start > 5000 or not 1 <= length <= 100:
		raise ValueError("pagination exceeds bounds")
	return start, length


def filters_for(value, fields):
	data = object_payload(value if value is not None else {}, fields)
	# Fixed equality filters only: callers cannot supply SQL operators or nested expressions.
	result = {}
	for field, val in data.items():
		if field == "enabled":
			if str(val) not in {"0", "1"}:
				raise ValueError("invalid enabled filter")
			result[field] = int(val)
		else:
			result[field] = normalize_reference(val, label=field)
	return result


def text_search(value):
	if value in (None, ""):
		return ""
	return normalize_required_text(value, label="search", maximum=140)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def save_version(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, MANAGER_ROLES, correlation_id):
		return planning.save_budget_version(build_version(payload), correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def save_plan(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, WRITE_ROLES, correlation_id):
		return planning.save_budget_plan(build_plan(payload), correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def freeze_version(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, MANAGER_ROLES, correlation_id):
		return planning.freeze_budget_version(build_action(payload), correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def clone_version(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, MANAGER_ROLES, correlation_id):
		return planning.clone_budget_version(
			build_action(payload, allow_subject=True), correlation_id=correlation_id
		)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def withdraw_plan(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, WRITE_ROLES, correlation_id):
		return planning.withdraw_budget_plan(build_action(payload), correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def upsert_master(kind: Any = None, payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, masters.WRITE_ROLES, correlation_id):
		return masters.upsert_budget_master(build_master(kind, payload), correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_record(kind: Any = None, name: Any = None, **extra):
	with request_guard(extra, READ_ROLES | masters.READ_ROLES):
		reference = normalize_reference(name, label="name")
		if isinstance(kind, str) and kind in MASTER_TYPES:
			return masters.get_budget_master(MASTER_TYPES[kind], reference).as_public_dict()
		return planning.read_budget(kind, reference)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def list_records(
	kind: Any = None, filters: Any = None, offset: Any = 0, limit: Any = 50, search: Any = None, **extra
):
	with request_guard(extra, READ_ROLES | masters.READ_ROLES):
		start, length = pagination(offset, limit)
		if not isinstance(kind, str):
			raise ValueError("invalid kind")
		fields = {
			"name",
			"company",
			"hospital",
			"fiscal_year",
			"status",
			"budget_version",
			"organization_unit",
		}
		if kind in MASTER_TYPES:
			fields = {"name", "company", "hospital", "dimension", "enabled"}
			reader = masters.list_budget_masters
		else:
			reader = planning.list_budget
		return reader(
			kind, filters_for(filters, fields), offset=start, limit=length, search=text_search(search)
		)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def spread(version: Any = None, amount: Any = None, **extra):
	with request_guard(extra, READ_ROLES):
		return {"items": planning.spread_budget_amount(normalize_reference(version, label="version"), amount)}


@frappe.whitelist(allow_guest=True, methods=["GET"])
def search(
	doctype: Any = None,
	txt: Any = "",
	searchfield: Any = None,
	start: Any = 0,
	page_len: Any = 20,
	filters: Any = None,
	as_dict: Any = False,
	reference_doctype: Any = None,
	ignore_user_permissions: Any = False,
	link_fieldname: Any = None,
	**extra,
):
	with request_guard(extra, READ_ROLES | masters.READ_ROLES):
		# Native search_widget supplies these protocol hints. They never bypass
		# current permissions or select SQL fields in this fixed facade.
		if ignore_user_permissions not in (False, 0, "0", None):
			raise ValueError("permission bypass is unsupported")
		for hint in (reference_doctype, link_fieldname):
			if hint not in (None, ""):
				normalize_reference(hint, label="search hint")
		if as_dict not in (False, 0, "0", None):
			raise ValueError("only native tuple search results are supported")
		kinds = {**{v: k for k, v in MASTER_TYPES.items()}, VERSION: "version", PLAN: "plan"}
		if not isinstance(doctype, str) or doctype not in kinds or searchfield not in (None, "name"):
			raise ValueError("unsupported planning search")
		# Native validate_link_and_fetch requests 25,000 candidates for a known
		# ID. Convert that protocol to one authorized equality lookup rather
		# than permitting an oversized scan or skipping current permissions.
		if normalize_nonnegative_integer(page_len, label="page_len") == 25000:
			if normalize_nonnegative_integer(start, label="start"):
				raise ValueError("exact validation starts at zero")
			fields = (
				{"name", "company", "hospital", "dimension", "enabled"}
				if doctype in MASTER_TYPES.values()
				else {
					"name",
					"company",
					"hospital",
					"fiscal_year",
					"status",
					"budget_version",
					"organization_unit",
				}
			)
			context = filters_for(filters, fields)
			reference = normalize_reference(txt, label="exact reference")
			if context.get("name", reference) != reference:
				return []
			filters = {**context, "name": reference}
			page_len, txt = 1, None
		response = list_records(
			kind=kinds[doctype], filters=filters, offset=start, limit=page_len, search=txt
		)
		return [
			[row["name"], row.get("display_name") or row.get("subject"), row.get("code") or row.get("status")]
			for row in response["items"]
		]


@frappe.whitelist(allow_guest=True, methods=["GET"])
def export_plan(name: Any = None, **extra):
	with request_guard(extra, READ_ROLES):
		doc = get_plan(normalize_reference(name, label="name"), export=True)
		fields = (
			"budget_subject",
			"funding_source",
			"account",
			"project",
			"period_index",
			"period_start",
			"period_end",
			"amount",
			"description",
		)
		emit_audit_event("budget_plan_exported", logger_name="medops.budget", line_count=len(doc.lines))
		return {
			"name": doc.name,
			"currency": doc.currency,
			"columns": list(fields),
			"rows": [[row.get(field) for field in fields] for row in doc.lines],
		}
