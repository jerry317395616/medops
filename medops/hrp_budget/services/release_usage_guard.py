"""Fail closed on reservation usage that a release reversal cannot yet replay."""

from __future__ import annotations

import frappe

from medops.common.budget_control import BudgetControlKey, ordered_budget_keys
from medops.hrp_budget.services.control_guards import assert_budget_guards, invalidate_budget_guards
from medops.services.errors import raise_ione_error

TABLES = ("tabHRP Budget Reservation", "tabHRP Budget Reservation Event")
UNSUPPORTED_TABLES = (
	"tabHRP Budget Adjustment",
	"tabHRP Budget Commitment",
	"tabHRP Budget Actualization",
)
QUERY_CHUNK = 100
# Python's reference normalizer strips Unicode whitespace and rejects control
# characters. Detect such physical values before matching canonical keys; a
# padded scope must not disappear from an equality predicate.
_UNICODE_SPACES = "\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"
_INVALID_REFERENCE = rf"(^[[:space:]{_UNICODE_SPACES}]|[[:space:]{_UNICODE_SPACES}]$|[[:cntrl:]])"


def _chunks(values, size=QUERY_CHUNK):
	for index in range(0, len(values), size):
		yield values[index : index + size]


def _table_engines() -> tuple[str, ...]:
	names = TABLES + UNSUPPORTED_TABLES
	placeholders = ", ".join(["%s"] * len(names))
	rows = frappe.db.sql(
		f"""SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES
		WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders})""",  # nosec B608 - bound names
		names,
	)
	engines = {str(name): str(engine).upper() for name, engine in rows}
	if any(engines.get(name) != "INNODB" for name in TABLES) or any(
		name in engines and engines[name] != "INNODB" for name in UNSUPPORTED_TABLES
	):
		raise_ione_error("CONFIGURATION_INVALID")
	return tuple(name for name in UNSUPPORTED_TABLES if name in engines)


def _has_unsupported_rows(table: str) -> bool:
	if table not in UNSUPPORTED_TABLES:
		raise ValueError("unsupported budget usage table")
	return bool(frappe.db.sql(f"SELECT 1 FROM `{table}` LIMIT 1"))  # nosec B608 - fixed table registry


def _has_unscoped_rows(table: str) -> bool:
	# The table identifier is restricted to a fixed internal registry.
	if table not in TABLES:
		raise ValueError("unsupported budget usage table")
	return bool(
		frappe.db.sql(
			f"""SELECT 1 FROM `{table}` WHERE
			`company` IS NULL OR `company`='' OR `hospital` IS NULL OR `hospital`=''
			OR `organization_unit` IS NULL OR `organization_unit`=''
			OR `company` REGEXP %s OR `hospital` REGEXP %s OR `organization_unit` REGEXP %s
			LIMIT 1""",  # nosec B608 - fixed table and bound pattern
			(_INVALID_REFERENCE,) * 3,
		)
	)


def _has_malformed_unit_rows(table: str, units: list[tuple[str, str, str]]) -> bool:
	if table not in TABLES:
		raise ValueError("unsupported budget usage table")
	for chunk in _chunks(units):
		clauses = " OR ".join(["(`company`=%s AND `hospital`=%s AND `organization_unit`=%s)"] * len(chunk))
		parameters = tuple(value for unit in chunk for value in unit) + (_INVALID_REFERENCE,) * 3
		if frappe.db.sql(
			f"""SELECT 1 FROM `{table}` WHERE ({clauses}) AND
			(`budget_subject` IS NULL OR `budget_subject`='' OR `funding_source` IS NULL OR `funding_source`=''
			OR `budget_subject` REGEXP %s OR `funding_source` REGEXP %s OR `project` REGEXP %s
			OR `period_start` IS NULL OR `period_end` IS NULL OR `period_start`>`period_end`) LIMIT 1""",  # nosec B608 - fixed table and placeholders
			parameters,
		):
			return True
	return False


def _has_overlapping_usage(table: str, keys: tuple[BudgetControlKey, ...]) -> bool:
	if table not in TABLES:
		raise ValueError("unsupported budget usage table")
	condition = """(`company`=%s AND `hospital`=%s AND `organization_unit`=%s
		AND `budget_subject`=%s AND `funding_source`=%s AND COALESCE(`project`, '')=%s
		AND `period_start`<=%s AND `period_end`>=%s)"""
	for chunk in _chunks(keys):
		clauses = " OR ".join([condition] * len(chunk))
		parameters = tuple(
			value
			for key in chunk
			for value in (
				key.company,
				key.hospital,
				key.organization_unit,
				key.budget_subject,
				key.funding_source,
				key.project or "",
				key.period_end,
				key.period_start,
			)
		)
		if frappe.db.sql(
			f"SELECT 1 FROM `{table}` WHERE {clauses} LIMIT 1",  # nosec B608 - fixed table and placeholders
			parameters,
		):
			return True
	return False


def require_valid_usage_storage(value: object) -> tuple[BudgetControlKey, ...]:
	"""Check guarded storage and malformed unit rows, without authorizing use."""
	try:
		keys = ordered_budget_keys(value)
		assert_budget_guards(keys)
		unsupported = _table_engines()
		for table in unsupported:
			if _has_unsupported_rows(table):
				raise_ione_error("INVALID_STATE_TRANSITION")
		units = sorted({(key.company, key.hospital, key.organization_unit) for key in keys})
		for table in TABLES:
			if _has_unscoped_rows(table) or _has_malformed_unit_rows(table, units):
				raise_ione_error("CONFIGURATION_INVALID")
		assert_budget_guards(keys)
		return keys
	except BaseException:
		invalidate_budget_guards()
		raise


def assert_no_unreconciled_reversal_usage(value: object) -> None:
	"""Deny reversal when the guarded scope has unreconciled usage evidence.

	This checks only the current physical tables and returns no row data. It is a
	temporary fail-closed policy, not a balance calculation. Future adjustment,
	commitment or actualization writers must extend this protocol before their
	tables are enabled. All release, reversal and usage writers must use the same
	guard, with old workers stopped before deployment; otherwise phantoms remain.
	The caller must fully roll back the transaction after any failure.
	"""
	try:
		keys = require_valid_usage_storage(value)
		for table in TABLES:
			if _has_overlapping_usage(table, keys):
				raise_ione_error("INVALID_STATE_TRANSITION")
		assert_budget_guards(keys)
	except BaseException:
		invalidate_budget_guards()
		raise
