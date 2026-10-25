from __future__ import annotations

from medops.services.core_reports import execute_core_report


def execute(filters=None):
	columns, rows = execute_core_report("RPT-006", filters)
	return (
		columns,
		rows,
		"按当前可读问题集合及完整事件权限统计采集历史；采集前和未来日期不可用，当日数据持续采集中。迁移基线不会虚构过去新增量。",
	)
