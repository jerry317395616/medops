from __future__ import annotations

from medops.services.core_reports import execute_core_report


def execute(filters=None):
	columns, rows = execute_core_report("RPT-002", filters)
	return columns, rows, "展示当前授权的组织映射及实际ERP部门、成本中心名称和停用状态。"
