from __future__ import annotations

import json
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from medops.common.domain_service import canonical_json, fingerprint_json
from medops.common.organization import (
	normalize_boolean,
	normalize_nonnegative_integer,
	normalize_optional_date,
	normalize_optional_text,
	normalize_required_date,
	normalize_required_text,
	validate_date_range,
)

REFERENCE = "HRP Reference Link"
QUALITY_EVENT = "HRP Data Quality Event"
MAX_REPORT_RECORDS = 5000
MAX_REFERENCE_BATCH = 100
EXPORT_MARKER = "_medops_export"
REFERENCE_SCOPE_FIELDS = frozenset({"company", "hospital", "organization_unit", "master_data_domain"})
REFERENCE_ENTRY_FIELDS = frozenset({"target_name", "enabled", "valid_from", "valid_to", "expected_revision"})
FILTER_FIELDS = frozenset(
	{
		"company",
		"hospital",
		"organization_unit",
		"from_date",
		"to_date",
		"as_of",
		"from_version",
		"to_version",
		"organization_version",
		"master_data_domain",
		"external_system",
		"status",
		"matrix",
		"from_user",
		"to_user",
		"target_doctype",
		"docname",
		"action",
		"subject_user",
	}
)
DUPLICATE_FIELDS = {
	"Department": "department_name",
	"Cost Center": "cost_center_name",
	"Item": "item_name",
	"Supplier": "supplier_name",
	"Warehouse": "warehouse_name",
}
REPORTS = {
	"RPT-001": (
		"HRP Organization Version Difference",
		"组织版本差异表",
		"HRP Organization",
		"HRP Organization Version",
	),
	"RPT-002": (
		"HRP Organization Standard Mapping",
		"组织与ERPNext部门映射表",
		"HRP Organization",
		"HRP Organization Mapping",
	),
	"RPT-003": (
		"HRP Master Data Turnaround",
		"主数据变更处理时效",
		"HRP Master Data",
		"HRP Master Data Request",
	),
	"RPT-004": ("HRP Master Data Duplicate Values", "主数据重复值检查", "HRP Master Data", REFERENCE),
	"RPT-005": ("HRP External Mapping Completeness", "外部编码映射完整率", "HRP Master Data", REFERENCE),
	"RPT-006": ("HRP Data Quality Issue Trend", "数据质量问题趋势", "HRP Master Data", QUALITY_EVENT),
	"RPT-007": (
		"HRP Delegation and Expiry Register",
		"审批委托与超期清单",
		"HRP Workflow Authorization",
		"HRP Delegation",
	),
	"RPT-008": (
		"HRP Segregation Screening",
		"不相容职责初筛",
		"HRP Workflow Authorization",
		"HRP Segregation Rule",
	),
}
_reference_write: ContextVar[bool] = ContextVar("medops_reference_write", default=False)
_history_write: ContextVar[bool] = ContextVar("medops_quality_history_write", default=False)


@contextmanager
def reference_write():
	token = _reference_write.set(True)
	try:
		yield
	finally:
		_reference_write.reset(token)


@contextmanager
def history_write():
	token = _history_write.set(True)
	try:
		yield
	finally:
		_history_write.reset(token)


def reference_write_allowed() -> bool:
	return _reference_write.get()


def history_write_allowed() -> bool:
	return _history_write.get()


def bounded_object(value: object, *, maximum: int = 16384) -> dict[str, Any]:
	if isinstance(value, str):
		if len(value.encode("utf-8")) > maximum:
			raise ValueError("JSON input exceeds limit")
		value = json.loads(value)
	if not isinstance(value, dict) or len(canonical_json(value).encode("utf-8")) > maximum:
		raise ValueError("bounded object required")
	return dict(value)


def report_filters(value: object, *, internal: bool = False) -> dict[str, Any]:
	data = bounded_object(value or {})
	if set(data) - FILTER_FIELDS - ({EXPORT_MARKER} if internal else set()):
		raise ValueError("unknown report filters")
	result: dict[str, Any] = {}
	for key, item in data.items():
		if key == EXPORT_MARKER:
			result[key] = normalize_boolean(item, label=key)
		elif key in {"from_date", "to_date", "as_of"}:
			result[key] = normalize_optional_date(item, label=key)
		else:
			result[key] = normalize_optional_text(item, label=key, maximum=140)
	if result.get("organization_unit") and not result.get("hospital"):
		raise ValueError("organization filter requires hospital")
	if result.get("hospital") and not result.get("company"):
		raise ValueError("hospital filter requires company")
	if bool(result.get("from_date")) != bool(result.get("to_date")):
		raise ValueError("both date bounds are required")
	if result.get("from_date"):
		validate_date_range(result["from_date"], result["to_date"])
		if (date.fromisoformat(result["to_date"]) - date.fromisoformat(result["from_date"])).days > 365:
			raise ValueError("date range exceeds one year")
	return {k: v for k, v in result.items() if v is not None and v != ""}


def reference_batch(scope: object, entries: object) -> dict[str, Any]:
	context = bounded_object(scope)
	if set(context) - REFERENCE_SCOPE_FIELDS:
		raise ValueError("unknown reference scope")
	context = {
		key: normalize_required_text(context.get(key), label=key, maximum=140)
		for key in ("company", "hospital", "master_data_domain")
	} | {
		"organization_unit": normalize_optional_text(
			context.get("organization_unit"), label="organization_unit", maximum=140
		)
	}
	if isinstance(entries, str):
		if len(entries.encode("utf-8")) > 65536:
			raise ValueError("reference batch exceeds limit")
		entries = json.loads(entries)
	if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_REFERENCE_BATCH:
		raise ValueError("one to one hundred entries required")
	result = []
	seen = set()
	for entry in entries:
		if not isinstance(entry, dict) or set(entry) - REFERENCE_ENTRY_FIELDS:
			raise ValueError("unknown reference entry fields")
		row = {
			"target_name": normalize_required_text(
				entry.get("target_name"), label="target_name", maximum=140
			),
			"enabled": int(normalize_boolean(entry.get("enabled", True), label="enabled")),
			"valid_from": normalize_required_date(entry.get("valid_from"), label="valid_from"),
			"valid_to": normalize_optional_date(entry.get("valid_to"), label="valid_to"),
			"expected_revision": normalize_nonnegative_integer(
				entry.get("expected_revision", 0), label="expected_revision"
			),
		}
		validate_date_range(row["valid_from"], row["valid_to"])
		if row["target_name"] in seen:
			raise ValueError("duplicate target in batch")
		seen.add(row["target_name"])
		result.append(row)
	return {"scope": context, "entries": sorted(result, key=lambda r: r["target_name"])}


def reference_identity(values: dict[str, Any]) -> str:
	return fingerprint_json(
		{
			key: values.get(key) or ""
			for key in (*sorted(REFERENCE_SCOPE_FIELDS), "target_doctype", "target_name")
		}
	)


def normalized_duplicate_value(value: object) -> str:
	if value is None:
		return ""
	if not isinstance(value, str) or len(value) > 500:
		raise ValueError("bounded master-data text required")
	return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def elapsed_minutes(start: datetime | None, end: datetime | None) -> float | None:
	if start is None or end is None:
		return None
	if end < start:
		raise ValueError("invalid event chronology")
	return float((Decimal(str((end - start).total_seconds())) / Decimal(60)).quantize(Decimal(".01")))


def quality_trend(
	daily: list[dict[str, Any]],
	*,
	start: date,
	end: date,
	epoch: datetime,
	opening: int = 0,
	observed_until: date | None = None,
) -> list[dict[str, Any]]:
	if end < start or (end - start).days > 365 or len(daily) > 366 or opening < 0:
		raise ValueError("trend exceeds bounds")
	by_day = {str(row["day"]): row for row in daily}
	rows = []
	day = start
	current = opening
	while day <= end:
		available = day >= epoch.date() and (observed_until is None or day <= observed_until)
		values = by_day.get(day.isoformat(), {})
		counts = {
			key: int(values.get(key) or 0)
			for key in ("baseline_open", "opened", "reopened", "resolved", "repeated")
		}
		current += counts["baseline_open"] + counts["opened"] + counts["reopened"] - counts["resolved"]
		if current < 0:
			raise ValueError("inconsistent quality history")
		coverage = "Unavailable"
		if available:
			coverage = "Recorded"
			if day == epoch.date() and epoch.time() != datetime.min.time():
				coverage = "Partial"
			if day == observed_until:
				coverage = "Recording"
		rows.append(
			{
				"date": day.isoformat(),
				"coverage": coverage,
				**{key: value if available else None for key, value in counts.items()},
				"open_at_end": current if available else None,
			}
		)
		day += timedelta(days=1)
	return rows
