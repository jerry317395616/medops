from __future__ import annotations

from medops.services.core_reports import execute_core_report


def execute(filters=None):
	columns, rows = execute_core_report("RPT-008", filters)
	return (
		columns,
		rows,
		"只读地检查一个真实单据、用户和动作的当前职责冲突；结果不授予权限，也不替代执行时复核。",
	)
