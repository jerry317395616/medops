"""Private bounded release evidence discovery under existing budget guards."""

from __future__ import annotations

import frappe

from medops.common.budget_control import ordered_budget_keys
from medops.common.budget_reservation_events import MAX_RESERVATION_EVENTS, ReservationPosition
from medops.common.reservation_release import RELEASE_ROW_FIELDS
from medops.hrp_budget.services.control_guards import assert_budget_guards
from medops.services.errors import raise_ione_error

DOCTYPE = "HRP Budget Reservation Release"
TABLE = "tabHRP Budget Reservation Release"
MAX_RELEASE_DECISIONS = 100000
QUERY_CHUNK = 100


def _columns():
	return ", ".join(f"`{field}`" for field in sorted(RELEASE_ROW_FIELDS))


def require_release_evidence_storage():
	"""Require physical InnoDB storage, scope columns and unique successor keys."""
	# MariaDB 11.2+ also reports session-local shadows in TABLES. Validate the
	# persisted base table explicitly, not an ambiguous set of same-name entries.
	engine = frappe.db.sql(
		"SELECT ENGINE FROM information_schema.TABLES "
		"WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND TABLE_TYPE='BASE TABLE'",
		(TABLE,),
	)
	columns = frappe.db.sql(
		"SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
		(TABLE,),
	)
	indexes = frappe.db.sql(
		"""SELECT COLUMN_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX FROM information_schema.STATISTICS
		WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s""",
		(TABLE,),
		as_dict=True,
	)
	unique = {}
	for row in indexes:
		if not int(row.NON_UNIQUE):
			unique.setdefault(row.INDEX_NAME, []).append((int(row.SEQ_IN_INDEX), row.COLUMN_NAME))
	unique_columns = {value[0][1] for value in unique.values() if len(value) == 1 and value[0][0] == 1}
	first_columns = {row.COLUMN_NAME for row in indexes if int(row.SEQ_IN_INDEX) == 1}
	if (
		len(engine) != 1
		or engine[0][0] != "InnoDB"
		or not RELEASE_ROW_FIELDS <= {row[0] for row in columns}
		or not {"evidence_key", "confirmation", "one_time_key", "event_digest"} <= unique_columns
		or not {"reservation", "company", "budget_subject"} <= first_columns
	):
		raise_ione_error("CONFIGURATION_INVALID")


def _merge(target, rows):
	page_names = set()
	for row in rows:
		name = row.get("name")
		if not isinstance(name, str) or not name or name in page_names or row.get("event_kind") != "Decision":
			raise ValueError("canonical release decision identity and kind required")
		page_names.add(name)
		if name in target and target[name] != row:
			raise ValueError("release decision changed between physical reads")
		target[name] = row
		if len(target) > MAX_RELEASE_DECISIONS:
			raise ValueError("release decision discovery exceeds bound")


def _by_reservations(names, *, limit):
	placeholders = ", ".join(["%s"] * len(names))
	return frappe.db.sql(
		f"SELECT {_columns()} FROM `{TABLE}` WHERE `reservation` IN ({placeholders}) "  # nosec B608 - fixed identifiers
		"AND `event_kind`=%s ORDER BY `reservation`, `previous_sequence`, `name` LIMIT %s FOR UPDATE",
		(*names, "Decision", limit),
		as_dict=True,
	)


def guarded_release_decision_inventory(keys, reservations):
	"""Find decisions by complete budget scope and by every known parent.

	Callers must check current ledger scope/field access before this private read,
	verify each decision's independent confirmation, and roll back any failure.
	This function does not grant access or authorize an event.
	"""
	ordered = ordered_budget_keys(keys)
	assert_budget_guards(ordered)
	require_release_evidence_storage()
	if not isinstance(reservations, (tuple, list)) or len(set(reservations)) != len(reservations):
		raise ValueError("unique reservation identities required")
	if len(reservations) > 10000 or any(not isinstance(name, str) or not name for name in reservations):
		raise ValueError("bounded reservation identities required")
	scopes = sorted(
		{
			(
				key.company,
				key.hospital,
				key.organization_unit,
				key.budget_subject,
				key.funding_source,
				key.project or "",
			)
			for key in ordered
		}
	)
	condition = """(`company`=%s AND `hospital`=%s AND `organization_unit`=%s
		AND `budget_subject`=%s AND `funding_source`=%s AND COALESCE(`project`, '')=%s)"""
	found = {}
	for start in range(0, len(scopes), QUERY_CHUNK):
		chunk = scopes[start : start + QUERY_CHUNK]
		clauses = " OR ".join([condition] * len(chunk))
		rows = frappe.db.sql(
			f"SELECT {_columns()} FROM `{TABLE}` WHERE ({clauses}) AND `event_kind`=%s "  # nosec B608 - fixed fields/conditions
			"ORDER BY `name` LIMIT %s FOR UPDATE",
			(*(value for scope in chunk for value in scope), "Decision", MAX_RELEASE_DECISIONS + 1),
			as_dict=True,
		)
		_merge(found, rows)
	for start in range(0, len(reservations), QUERY_CHUNK):
		_merge(
			found,
			_by_reservations(reservations[start : start + QUERY_CHUNK], limit=MAX_RELEASE_DECISIONS + 1),
		)
	assert_budget_guards(ordered)
	return found


def locked_release_pairs(keys, positions):
	"""Read all posted decisions and their confirmations for one bounded root batch."""
	ordered = ordered_budget_keys(keys)
	assert_budget_guards(ordered)
	require_release_evidence_storage()
	if (
		not isinstance(positions, (tuple, list))
		or not 1 <= len(positions) <= QUERY_CHUNK
		or any(type(position) is not ReservationPosition for position in positions)
		or any(
			type(position.last_sequence) is not int
			or not 1 <= position.last_sequence <= MAX_RESERVATION_EVENTS
			for position in positions
		)
	):
		raise ValueError("bounded complete reservation positions required")
	names = tuple(position.reservation for position in positions)
	if len(set(names)) != len(names):
		raise ValueError("unique reservation positions required")
	expected_count = sum(position.last_sequence - 1 for position in positions)
	if expected_count > MAX_RELEASE_DECISIONS:
		raise ValueError("release confirmation batch exceeds bound")
	decisions = _by_reservations(names, limit=expected_count + 1)
	if len(decisions) != expected_count:
		raise ValueError("all posted release decisions must match physical event tails")
	by_name = {name: [] for name in names}
	confirmation_names = []
	for row in decisions:
		if row["reservation"] not in by_name or row["event_kind"] != "Decision":
			raise ValueError("release decision parent or kind differs")
		by_name[row["reservation"]].append(row)
		confirmation_names.append(row["confirmation"])
	if any(len(by_name[position.reservation]) != position.last_sequence - 1 for position in positions):
		raise ValueError("release decision count differs from each reservation history")
	if any(not isinstance(name, str) or not name for name in confirmation_names) or len(
		set(confirmation_names)
	) != len(confirmation_names):
		raise ValueError("unique release confirmation references required")
	confirmations = {}
	for start in range(0, len(confirmation_names), QUERY_CHUNK):
		chunk = confirmation_names[start : start + QUERY_CHUNK]
		placeholders = ", ".join(["%s"] * len(chunk))
		rows = frappe.db.sql(
			f"SELECT {_columns()} FROM `{TABLE}` WHERE `name` IN ({placeholders}) "  # nosec B608 - fixed identifiers
			"ORDER BY `name` FOR UPDATE",
			tuple(chunk),
			as_dict=True,
		)
		for row in rows:
			if row["name"] in confirmations:
				raise ValueError("release confirmation cannot be reused")
			confirmations[row["name"]] = row
	if set(confirmations) != set(confirmation_names):
		raise ValueError("complete independent release confirmation evidence required")
	assert_budget_guards(ordered)
	return {
		name: [(confirmations[row["confirmation"]], row) for row in rows] for name, rows in by_name.items()
	}
