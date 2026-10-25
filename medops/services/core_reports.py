from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, cast

import frappe
from frappe.utils import get_datetime, today

from medops.common.core_reports import (
	DUPLICATE_FIELDS,
	EXPORT_MARKER,
	QUALITY_EVENT,
	REPORTS,
	elapsed_minutes,
	normalized_duplicate_value,
	quality_trend,
	report_filters,
)
from medops.common.segregation import build_segregation_evaluation
from medops.hrp_foundation.services.reference_links import visible_references
from medops.hrp_master_data.services.access import target_readable
from medops.hrp_master_data.services.quality_history import capture_epoch
from medops.hrp_organization.services.organization import get_organization_hierarchy
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.hrp_workflow_authorization.services.segregation import validate_segregation
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.core_report_access import require_fields, scoped_query, visible_documents
from medops.services.data_access import require_scope, scope_allowed
from medops.services.errors import raise_ione_error, require_authenticated_user


def column(key, label, kind="Data", options=None):
	result = {"fieldname": key, "label": label, "fieldtype": kind, "width": 160}
	if options:
		result["options"] = options
	return result


def scope_filters(filters):
	return {key: filters[key] for key in ("company", "hospital", "organization_unit") if filters.get(key)}


def dated(filters, field):
	if filters.get("from_date"):
		return {
			field: ("between", [filters["from_date"] + " 00:00:00", filters["to_date"] + " 23:59:59.999999"])
		}
	return {}


def readable(doc, fields, export=False):
	return (
		can_read_named_fields(doc, fields)
		and (not export or frappe.has_permission(doc.doctype, "export", doc=doc))
		and scope_allowed(doc, action="export" if export else "read")
	)


def organization_difference(filters, export):
	if not filters.get("from_version") or not filters.get("to_version"):
		raise_ione_error("INVALID_REQUEST")
	versions = []
	for key in ("from_version", "to_version"):
		snapshot = get_organization_hierarchy(organization_version=filters[key])
		version = frappe.get_doc("HRP Organization Version", filters[key])
		require_fields(version, {"status", "company", "hospital", "node_count"}, export=export)
		if version.status != "Published":
			raise_ione_error("INVALID_STATE_TRANSITION")
		if any(version.get(k) != v for k, v in scope_filters(filters).items()):
			raise_ione_error("INVALID_REQUEST")
		if export:
			for row in frappe.get_list(
				"HRP Organization Unit",
				fields=["name"],
				filters={"organization_version": version.name},
				limit=0,
			):
				node = frappe.get_doc("HRP Organization Unit", row.name)
				require_fields(node, {f.fieldname for f in node.meta.fields}, export=True)
		versions.append(
			(version, {row["code"]: row for row in cast(list[dict[str, Any]], snapshot["nodes"])})
		)
	if versions[0][0].company != versions[1][0].company or versions[0][0].hospital != versions[1][0].hospital:
		raise_ione_error("INVALID_REQUEST")
	rows = []
	before = versions[0][1]
	after = versions[1][1]
	for code in sorted(before.keys() | after.keys()):
		old = before.get(code)
		new = after.get(code)
		fields = sorted(key for key in (old or new) if (old or {}).get(key) != (new or {}).get(key))
		if fields:
			rows.append(
				{
					"code": code,
					"change": "Added" if old is None else "Removed" if new is None else "Changed",
					"fields": ", ".join(fields),
					"before": json.dumps(old, ensure_ascii=False, sort_keys=True) if old else None,
					"after": json.dumps(new, ensure_ascii=False, sort_keys=True) if new else None,
				}
			)
	return [
		column("code", "组织编码"),
		column("change", "差异"),
		column("fields", "变更字段"),
		column("before", "原版本内容"),
		column("after", "新版本内容"),
	], rows


def organization_mapping(filters, export):
	query = scope_filters(filters)
	if filters.get("organization_version"):
		query["organization_version"] = filters["organization_version"]
	rows = []
	fields = {
		"name",
		"organization_version",
		"organization_unit",
		"unit_code",
		"department",
		"cost_center",
		"enabled",
		"company",
		"hospital",
	}
	for doc in visible_documents("HRP Organization Mapping", query, export=export):
		if not readable(doc, fields, export):
			continue
		labels = {}
		allowed = True
		for key, dt, label in (
			("department", "Department", "department_name"),
			("cost_center", "Cost Center", "cost_center_name"),
		):
			name = doc.get(key)
			if name and not target_readable(
				dt,
				name,
				fields={label, "disabled"},
				company=doc.company,
				action="export" if export else "read",
			):
				allowed = False
				break
			labels[key + "_label"] = frappe.db.get_value(dt, name, label) if name else None
			labels[key + "_disabled"] = frappe.db.get_value(dt, name, "disabled") if name else None
		if allowed:
			rows.append({**{key: doc.get(key) for key in fields}, **labels})
	return [
		column("name", "映射", "Link", "HRP Organization Mapping"),
		column("organization_version", "组织版本", "Link", "HRP Organization Version"),
		column("unit_code", "组织编码"),
		column("department", "ERP部门", "Link", "Department"),
		column("department_label", "部门名称"),
		column("department_disabled", "部门停用", "Check"),
		column("cost_center", "成本中心", "Link", "Cost Center"),
		column("cost_center_label", "成本中心名称"),
		column("cost_center_disabled", "中心停用", "Check"),
		column("enabled", "映射启用", "Check"),
	], rows


def turnaround(filters, export):
	query = {**scope_filters(filters), **dated(filters, "submitted_at")}
	if filters.get("status"):
		query["request_status"] = filters["status"]
	fields = {
		"name",
		"subject",
		"request_status",
		"requested_by",
		"submitted_at",
		"reviewed_at",
		"reviewed_by",
	}
	rows = []
	for doc in visible_documents("HRP Master Data Request", query, export=export):
		if not readable(doc, fields, export):
			continue
		try:
			duration = elapsed_minutes(
				get_datetime(doc.submitted_at) if doc.submitted_at else None,
				get_datetime(doc.reviewed_at) if doc.reviewed_at else None,
			)
		except ValueError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		rows.append({**{key: doc.get(key) for key in fields}, "elapsed_minutes": duration})
	return [
		column("name", "申请", "Link", "HRP Master Data Request"),
		column("subject", "事项"),
		column("request_status", "状态"),
		column("requested_by", "申请人", "Link", "User"),
		column("submitted_at", "提交时间", "Datetime"),
		column("reviewed_at", "复核时间", "Datetime"),
		column("reviewed_by", "复核人", "Link", "User"),
		column("elapsed_minutes", "提交至复核自然分钟", "Float"),
	], rows


def population(filters, export):
	if not filters.get("company") or not filters.get("hospital") or not filters.get("master_data_domain"):
		raise_ione_error("INVALID_REQUEST")
	query = {**scope_filters(filters), "master_data_domain": filters["master_data_domain"], "enabled": 1}
	as_of = filters.get("as_of") or today()
	return [
		doc
		for doc in visible_references(query, export=export)
		if str(doc.valid_from) <= as_of and (not doc.valid_to or str(doc.valid_to) >= as_of)
	]


def duplicates(filters, export):
	groups = defaultdict(list)
	for doc in population(filters, export):
		field = DUPLICATE_FIELDS[doc.target_doctype]
		if not target_readable(
			doc.target_doctype,
			doc.target_name,
			fields={field},
			company=doc.company,
			action="export" if export else "read",
		):
			continue
		value = frappe.db.get_value(doc.target_doctype, doc.target_name, field)
		normalized = normalized_duplicate_value(value)
		if normalized:
			groups[(doc.target_doctype, normalized)].append((doc.target_name, value))
	rows = []
	for (dt, value), members in sorted(groups.items()):
		members = sorted(set(members))
		if len(members) < 2:
			continue
		for name, original in members:
			rows.append(
				{
					"target_doctype": dt,
					"target_name": name,
					"value": original,
					"normalized_value": value,
					"duplicate_count": len(members),
				}
			)
	return [
		column("target_doctype", "标准类型"),
		column("target_name", "主数据编号"),
		column("value", "当前名称"),
		column("normalized_value", "规范化名称"),
		column("duplicate_count", "授权登记内重复数", "Int"),
	], rows


def completeness(filters, export):
	if not filters.get("external_system"):
		raise_ione_error("INVALID_REQUEST")
	if not frappe.has_permission("HRP External Code Mapping", "read"):
		raise_ione_error("PERMISSION_DENIED")
	require_scope(
		"HRP External Code Mapping",
		company=filters.get("company"),
		hospital=filters.get("hospital"),
		organization_unit=filters.get("organization_unit"),
		action="export" if export else "read",
	)
	probe = frappe.get_doc({"doctype": "HRP External Code Mapping", "owner": ""})
	if not can_read_named_fields(probe, {f.fieldname for f in probe.meta.fields}):
		raise_ione_error("PERMISSION_DENIED")
	refs = population(filters, export)
	as_of = filters.get("as_of") or today()
	refs = [
		doc
		for doc in refs
		if target_readable(
			doc.target_doctype,
			doc.target_name,
			fields={"disabled"},
			company=doc.company,
			action="export" if export else "read",
		)
	]
	query = {
		**scope_filters(filters),
		"master_data_domain": filters["master_data_domain"],
		"external_system": filters["external_system"],
		"enabled": 1,
	}
	mappings = visible_documents("HRP External Code Mapping", query, export=export)
	mapped = {
		(doc.target_doctype, doc.internal_name, doc.organization_unit or "")
		for doc in mappings
		if str(doc.valid_from) <= as_of and (not doc.valid_to or str(doc.valid_to) >= as_of)
	}
	# Count each registered object once; a hospital-wide mapping applies to a local registration.
	objects = defaultdict(list)
	for doc in refs:
		objects[(doc.target_doctype, doc.target_name)].append(doc.organization_unit or "")
	count = sum(
		any((dt, name, unit) in mapped or (dt, name, "") in mapped for unit in units)
		for (dt, name), units in objects.items()
	)
	total = len(objects)
	return [
		column("external_system", "外部系统"),
		column("as_of", "业务日期", "Date"),
		column("population", "当前授权登记数", "Int"),
		column("mapped", "有效映射数", "Int"),
		column("missing", "缺失映射数", "Int"),
		column("percent", "完整率（%）", "Percent"),
	], [
		{
			"external_system": filters["external_system"],
			"as_of": as_of,
			"population": total,
			"mapped": count,
			"missing": total - count,
			"percent": round(count * 100 / total, 2) if total else None,
		}
	]


def quality_history(filters, export):
	if not filters.get("from_date"):
		raise_ione_error("INVALID_REQUEST")
	if not frappe.has_permission("HRP Data Quality Issue", "read"):
		raise_ione_error("PERMISSION_DENIED")
	issues = visible_documents("HRP Data Quality Issue", scope_filters(filters), export=False)
	if export:
		from medops.hrp_master_data.services.access import master_data_visible

		for issue in issues:
			if not frappe.has_permission(issue.doctype, "export", doc=issue) or not master_data_visible(
				issue, action="export"
			):
				raise_ione_error("PERMISSION_DENIED")
	if filters.get("master_data_domain"):
		issues = [doc for doc in issues if doc.master_data_domain == filters["master_data_domain"]]
	start = date.fromisoformat(filters["from_date"])
	end = date.fromisoformat(filters["to_date"])
	epoch = capture_epoch()
	if not epoch:
		raise_ione_error("CONFIGURATION_INVALID")
	daily = []
	opening = 0
	if issues:
		fields = {
			"issue",
			"event_type",
			"from_status",
			"to_status",
			"occurred_at",
			"issue_revision",
			"access_fields",
		}
		probe = frappe.get_doc({"doctype": QUALITY_EVENT, "owner": ""})
		if not can_read_named_fields(probe, fields):
			raise_ione_error("PERMISSION_DENIED")
		if frappe.session.user != "Administrator":
			permissions = [
				p
				for p in probe.meta.permissions
				if p.role in frappe.get_roles() and not p.if_owner and not p.permlevel
			]
			if not any(p.read for p in permissions) or (export and not any(p.export for p in permissions)):
				raise_ione_error("PERMISSION_DENIED")
		for issue in issues:
			context = frappe.get_doc(
				{
					"doctype": QUALITY_EVENT,
					"company": issue.company,
					"hospital": issue.hospital,
					"organization_unit": issue.organization_unit,
				}
			)
			if not scope_allowed(context, action="export" if export else "read"):
				raise_ione_error("PERMISSION_DENIED")
		query = {
			"issue": ("in", [doc.name for doc in issues]),
			"occurred_at": ("<", datetime.combine(end + timedelta(days=1), time.min)),
		}
		with scoped_query():
			qb = frappe.qb.get_query(
				QUALITY_EVENT, fields=["name"], filters=query, ignore_permissions=False, limit=None
			)
			allowed, params = qb.walk()
		params.update(hrp_start=datetime.combine(start, time.min))
		params["hrp_issues"] = tuple(doc.name for doc in issues)
		params["hrp_end"] = datetime.combine(end + timedelta(days=1), time.min)
		if frappe.db.sql(
			f"SELECT name FROM `tabHRP Data Quality Event` WHERE issue IN %(hrp_issues)s AND occurred_at < %(hrp_end)s AND name NOT IN ({allowed}) LIMIT 1",
			params,
		):
			raise_ione_error("PERMISSION_DENIED")  # nosec B608 -- bound native permission query.
		# The subquery is generated by the locked Frappe permission engine, not caller SQL.
		base = f"FROM `tabHRP Data Quality Event` e WHERE e.name IN ({allowed})"
		bad = frappe.db.sql(
			"SELECT e.name "
			+ base
			+ " AND NOT EXISTS (SELECT 1 FROM `tabHRP Data Quality Issue` i WHERE i.name=e.issue AND CASE WHEN JSON_VALID(e.access_fields) AND JSON_VALID(i.evaluation_read_fields) THEN JSON_CONTAINS(i.evaluation_read_fields,e.access_fields) ELSE 0 END=1) LIMIT 1",
			params,
		)  # nosec B608 -- static SQL plus a Frappe-generated bound permission query.
		if bad:
			raise_ione_error("CONFIGURATION_INVALID")
		opening = int(
			frappe.db.sql(
				"SELECT COALESCE(SUM(last_state.to_status='Open'),0) FROM (SELECT e.to_status,ROW_NUMBER() OVER(PARTITION BY e.issue ORDER BY e.occurred_at DESC,e.issue_revision DESC) rank_in_issue "
				+ base
				+ " AND e.occurred_at < %(hrp_start)s) last_state WHERE rank_in_issue=1",
				params,
			)[0][0]
		)  # nosec B608
		daily = frappe.db.sql(
			"SELECT DATE(e.occurred_at) day,SUM(e.event_type='Baseline' AND e.to_status='Open') baseline_open,SUM(e.event_type='Detected') opened,SUM(e.event_type='Reopened') reopened,SUM(e.event_type='Resolved') resolved,SUM(e.event_type='Repeated') repeated "
			+ base
			+ " AND e.occurred_at >= %(hrp_start)s GROUP BY DATE(e.occurred_at) ORDER BY day",
			params,
			as_dict=True,
		)  # nosec B608
	rows = quality_trend(
		daily,
		start=start,
		end=end,
		epoch=get_datetime(epoch),
		opening=opening,
		observed_until=date.fromisoformat(today()),
	)
	return [
		column("date", "采集日期", "Date"),
		column("coverage", "历史覆盖"),
		*[
			column(key, label, "Int")
			for key, label in (
				("baseline_open", "既有未解决基线"),
				("opened", "首次发现"),
				("reopened", "重新打开"),
				("resolved", "解决"),
				("repeated", "重复发现"),
				("open_at_end", "日末未解决"),
			)
		],
	], rows


def delegation(filters, export):
	query = scope_filters(filters)
	for key in ("status", "matrix", "from_user", "to_user"):
		if filters.get(key):
			query[key] = filters[key]
	if filters.get("from_date"):
		query.update(valid_from=("<=", filters["to_date"]), valid_to=(">=", filters["from_date"]))
	fields = {
		"name",
		"status",
		"matrix",
		"target_doctype",
		"company",
		"hospital",
		"organization_unit",
		"from_user",
		"to_user",
		"valid_from",
		"valid_to",
	}
	rows = []
	for doc in visible_documents("HRP Delegation", query, export=export):
		if not readable(doc, fields, export):
			continue
		rows.append(
			{
				**{key: doc.get(key) for key in fields},
				"remaining_days": (date.fromisoformat(str(doc.valid_to)) - date.fromisoformat(today())).days,
			}
		)
	return [
		column("name", "委托", "Link", "HRP Delegation"),
		column("status", "状态"),
		column("matrix", "审批矩阵", "Link", "HRP Approval Matrix"),
		column("target_doctype", "适用单据类型"),
		column("company", "法人"),
		column("hospital", "医院"),
		column("organization_unit", "组织单元"),
		column("from_user", "委托人", "Link", "User"),
		column("to_user", "受托人", "Link", "User"),
		column("valid_from", "开始日期", "Date"),
		column("valid_to", "结束日期", "Date"),
		column("remaining_days", "剩余天数", "Int"),
	], rows


def segregation(filters, export):
	from medops.hrp_workflow_authorization.services.segregation import SEGREGATION_ADMIN_ROLES
	from medops.services.errors import require_roles

	require_roles(SEGREGATION_ADMIN_ROLES)
	if not all(filters.get(key) for key in ("subject_user", "action", "target_doctype", "docname")):
		raise_ione_error("INVALID_REQUEST")
	doc = frappe.get_doc(filters["target_doctype"], filters["docname"])
	if any(doc.get(k) != v for k, v in scope_filters(filters).items()):
		raise_ione_error("INVALID_REQUEST")
	user = frappe.get_doc("User", filters["subject_user"])
	require_fields(user, {"enabled", "user_type", "roles", "roles.role"})
	# Screen only after the fields behind inferred conflicts are readable.
	fields = {
		key
		for key in (
			"company",
			"hospital",
			"organization_unit",
			"posting_date",
			"effective_on",
			"business_date",
			"requested_by",
			"owner",
			"reviewed_by",
			"approved_by",
			"prepared_by",
			"submitted_by",
			"status",
			"docstatus",
		)
		if doc.meta.has_field(key) or key in {"owner", "docstatus"}
	}
	for row in frappe.get_list(
		"HRP Segregation Rule", fields=["name"], filters={"target_doctype": doc.doctype}, limit=0
	):
		rule = frappe.get_doc("HRP Segregation Rule", row.name)
		fields.update(rule.as_runtime_definition().actor_fields)
	require_fields(doc, fields, export=export)
	result = validate_segregation(
		build_segregation_evaluation(
			user=filters["subject_user"],
			action=filters["action"],
			doctype=doc.doctype,
			docname=doc.name,
		)
	)
	rows = [
		{
			"user": filters["subject_user"],
			"action": filters["action"],
			"allowed": result["allowed"],
			"rules_evaluated": result["rules_evaluated"],
			"conflict_count": len(cast(list[Any], result["conflicts"])),
			"decision_digest": result["decision_digest"],
		}
	]
	return [
		column("user", "被检查用户", "Link", "User"),
		column("action", "动作"),
		column("allowed", "当前允许", "Check"),
		column("rules_evaluated", "适用规则数", "Int"),
		column("conflict_count", "冲突数", "Int"),
		column("decision_digest", "本次摘要"),
	], rows


HANDLERS = {
	"RPT-001": organization_difference,
	"RPT-002": organization_mapping,
	"RPT-003": turnaround,
	"RPT-004": duplicates,
	"RPT-005": completeness,
	"RPT-006": quality_history,
	"RPT-007": delegation,
	"RPT-008": segregation,
}


def protect_fresh_reports(doc, method=None):
	"""Prepared report reuse bypasses live source checks; core reports cannot enable it."""
	del method
	if core_report_code(doc=doc):
		if doc.prepared_report:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if doc.get("json"):
			try:
				saved = frappe.parse_json(doc.json)
			except (ValueError, TypeError) as exc:
				raise_ione_error("INVALID_REQUEST", cause=exc)
			if isinstance(saved, dict):
				reject_added_sources(saved.get("columns"))


def core_report_code(name=None, *, doc=None):
	known = {entry[0]: code for code, entry in REPORTS.items()}
	seen = set()
	for _ in range(100):
		if doc is None:
			if not isinstance(name, str) or not frappe.db.exists("Report", name):
				return None
			doc = frappe.get_doc("Report", name)
		if doc.name in known:
			return known[doc.name]
		if doc.report_type != "Custom Report":
			return None
		if doc.name in seen:
			raise_ione_error("CONFIGURATION_INVALID")
		seen.add(doc.name)
		name = doc.reference_report
		doc = None
	raise_ione_error("CONFIGURATION_INVALID")


def reject_added_sources(columns):
	if not columns:
		return
	try:
		columns = frappe.parse_json(columns)
	except (ValueError, TypeError) as exc:
		raise_ione_error("INVALID_REQUEST", cause=exc)
	if not isinstance(columns, list) or any(
		not isinstance(row, dict) or row.get("link_field") for row in columns
	):
		raise_ione_error("INVALID_REQUEST")


FILTERS = {
	"RPT-001": {"company", "hospital", "from_version", "to_version"},
	"RPT-002": {"company", "hospital", "organization_unit", "organization_version"},
	"RPT-003": {"company", "hospital", "organization_unit", "from_date", "to_date", "status"},
	"RPT-004": {"company", "hospital", "organization_unit", "master_data_domain", "as_of"},
	"RPT-005": {"company", "hospital", "organization_unit", "master_data_domain", "as_of", "external_system"},
	"RPT-006": {"company", "hospital", "organization_unit", "master_data_domain", "from_date", "to_date"},
	"RPT-007": {
		"company",
		"hospital",
		"organization_unit",
		"status",
		"matrix",
		"from_user",
		"to_user",
		"from_date",
		"to_date",
	},
	"RPT-008": {
		"company",
		"hospital",
		"organization_unit",
		"subject_user",
		"action",
		"target_doctype",
		"docname",
	},
}


def execute_core_report(code: str, filters=None, *, export: bool | None = None):
	with service_audit_scope():
		require_authenticated_user()
		if code not in REPORTS:
			raise_ione_error("INVALID_REQUEST")
		try:
			data = report_filters(filters, internal=True)
		except (ValueError, TypeError) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		if set(data) - FILTERS[code] - {EXPORT_MARKER}:
			raise_ione_error("INVALID_REQUEST")
		requested_export = bool(data.pop(EXPORT_MARKER, False))
		if export is None:
			export = requested_export or not (
				getattr(frappe.local, "request", None)
				and frappe.form_dict.get("cmd") == "frappe.desk.query_report.run"
			)
		else:
			export = export or requested_export
		if (
			not frappe.get_doc("Report", REPORTS[code][0]).is_permitted()
			or not frappe.has_permission(REPORTS[code][3], "report")
			or not frappe.has_permission(REPORTS[code][3], "read")
		):
			raise_ione_error("PERMISSION_DENIED")
		columns, rows = HANDLERS[code](data, export)
		emit_audit_event(
			"core_report_read", logger_name="medops.reports", report=code, export=export, returned=len(rows)
		)
		return columns, rows


@frappe.whitelist()
def export_query() -> None:
	from frappe.desk.query_report import export_query as original

	name = frappe.form_dict.get("report_name")
	if core_report_code(name):
		reject_added_sources(frappe.form_dict.get("custom_columns"))
		try:
			filters = report_filters(frappe.parse_json(frappe.form_dict.get("filters")) or {}, internal=True)
		except (ValueError, TypeError) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		filters[EXPORT_MARKER] = True
		frappe.form_dict["filters"] = json.dumps(filters)
	return original()
