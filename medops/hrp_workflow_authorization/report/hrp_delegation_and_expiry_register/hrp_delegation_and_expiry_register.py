from __future__ import annotations

from datetime import date
from typing import Any

import frappe


def execute(
	filters: dict[str, object] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
	filters = filters or {}
	allowed = {
		key: value
		for key, value in filters.items()
		if key in {"status", "matrix", "from_user", "to_user"} and value
	}
	rows = frappe.get_list(
		"HRP Delegation",
		filters=allowed,
		fields=[
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
		],
		order_by="valid_to asc, name asc",
		limit_page_length=10000,
	)
	today = date.today()
	data: list[dict[str, object]] = []
	for row in rows:
		end = row.valid_to
		remaining = (
			(end - today).days if isinstance(end, date) else (date.fromisoformat(str(end)) - today).days
		)
		data.append({**dict(row), "remaining_days": remaining})
	return _columns(), data


def _columns() -> list[dict[str, Any]]:
	return [
		{
			"fieldname": "name",
			"label": "委托编号",
			"fieldtype": "Link",
			"options": "HRP Delegation",
			"width": 150,
		},
		{"fieldname": "status", "label": "状态", "fieldtype": "Data", "width": 90},
		{
			"fieldname": "matrix",
			"label": "审批矩阵",
			"fieldtype": "Link",
			"options": "HRP Approval Matrix",
			"width": 150,
		},
		{
			"fieldname": "target_doctype",
			"label": "适用单据类型",
			"fieldtype": "Link",
			"options": "DocType",
			"width": 140,
		},
		{"fieldname": "company", "label": "法人", "fieldtype": "Link", "options": "Company", "width": 140},
		{
			"fieldname": "hospital",
			"label": "医院",
			"fieldtype": "Link",
			"options": "HRP Hospital",
			"width": 130,
		},
		{
			"fieldname": "organization_unit",
			"label": "组织单元",
			"fieldtype": "Link",
			"options": "HRP Organization Unit",
			"width": 150,
		},
		{"fieldname": "from_user", "label": "委托人", "fieldtype": "Link", "options": "User", "width": 170},
		{"fieldname": "to_user", "label": "受托人", "fieldtype": "Link", "options": "User", "width": 170},
		{"fieldname": "valid_from", "label": "开始日期", "fieldtype": "Date", "width": 105},
		{"fieldname": "valid_to", "label": "结束日期", "fieldtype": "Date", "width": 105},
		{"fieldname": "remaining_days", "label": "剩余天数", "fieldtype": "Int", "width": 90},
	]


__all__ = ["execute"]
