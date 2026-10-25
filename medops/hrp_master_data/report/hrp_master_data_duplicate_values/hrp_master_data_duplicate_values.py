from __future__ import annotations

from medops.services.core_reports import execute_core_report


def execute(filters=None):
	columns, rows = execute_core_report("RPT-004", filters)
	return (
		columns,
		rows,
		"仅检查所选医院当前可见、在业务日期有效的明确登记对象及可读名称；不会自动把全局目录算作本院目录。",
	)
