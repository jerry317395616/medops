from __future__ import annotations

from medops.services.core_reports import execute_core_report


def execute(filters=None):
	columns, rows = execute_core_report("RPT-007", filters)
	return columns, rows
