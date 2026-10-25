from __future__ import annotations

from medops.services.core_reports import execute_core_report


def execute(filters=None):
	columns, rows = execute_core_report("RPT-005", filters)
	return (
		columns,
		rows,
		"按当前授权登记台账和所选业务日期的有效区间计算；不是过去时点快照。分母为授权且可检测的登记对象，零分母不计算百分比。",
	)
