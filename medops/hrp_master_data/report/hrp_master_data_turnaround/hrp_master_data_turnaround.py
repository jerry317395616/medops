from __future__ import annotations

from medops.services.core_reports import execute_core_report


def execute(filters=None):
	columns, rows = execute_core_report("RPT-003", filters)
	return columns, rows, "时效为实际提交至复核的自然分钟；尚未完成复核的申请不填完成时效。"
