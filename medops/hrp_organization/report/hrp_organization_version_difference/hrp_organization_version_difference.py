from __future__ import annotations

from medops.services.core_reports import execute_core_report


def execute(filters=None):
	columns, rows = execute_core_report("RPT-001", filters)
	return columns, rows, "比较同一医院两个已发布的完整组织版本；无法读取完整版本时不生成差异。"
