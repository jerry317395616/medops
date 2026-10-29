"""Bounded private reads of independent release/Commit evidence under shared guards."""

from __future__ import annotations

from dataclasses import dataclass, field

import frappe

from medops.common.budget_control import ordered_budget_keys
from medops.common.budget_reservation_events import MAX_RESERVATION_EVENTS, ReservationPosition
from medops.common.commitment_evidence import COMMITMENT_ROW_FIELDS
from medops.common.reservation_commitment_history import COMMIT_ACTION_PREFIX
from medops.common.reservation_release import RELEASE_ACTION_PREFIX, RELEASE_ROW_FIELDS
from medops.hrp_budget.services.commitment_authorization import require_commitment_evidence_storage
from medops.hrp_budget.services.control_guards import assert_budget_guards
from medops.hrp_budget.services.reservation_release_observation import require_release_evidence_storage

QUERY_CHUNK = 100
MAX_DECISIONS = 100000
COMMITMENT_TABLE = "tabHRP Budget Commitment Authorization"
RELEASE_TABLE = "tabHRP Budget Reservation Release"


def _spec(kind):
	if kind == "Commit":
		return COMMITMENT_TABLE, COMMITMENT_ROW_FIELDS, COMMIT_ACTION_PREFIX
	if kind == "Release Reservation":
		return RELEASE_TABLE, RELEASE_ROW_FIELDS, RELEASE_ACTION_PREFIX
	raise ValueError("unsupported independent reservation action")


def _columns(fields):
	return ", ".join(f"`{name}`" for name in sorted(fields))


def guarded_commitment_decision_inventory(keys, reservations):
	"""Read scope and reverse-parent decisions, including orphan decisions.

	The caller must first check current ledger scope/field access. Physical
	discovery is not authorization and every result needs independent validation.
	"""
	ordered = ordered_budget_keys(keys)
	assert_budget_guards(ordered)
	require_commitment_evidence_storage()
	if (
		not isinstance(reservations, (tuple, list))
		or len(reservations) > 10000
		or any(not isinstance(name, str) or not name for name in reservations)
		or len(set(reservations)) != len(reservations)
	):
		raise ValueError("unique bounded reservation names required")
	scopes = sorted(
		{
			(k.company, k.hospital, k.organization_unit, k.budget_subject, k.funding_source, k.project or "")
			for k in ordered
		}
	)
	condition = """(`company`=%s AND `hospital`=%s AND `organization_unit`=%s
		AND `budget_subject`=%s AND `funding_source`=%s AND COALESCE(`project`, '')=%s)"""
	found = {}

	def merge(rows):
		page = set()
		for row in rows:
			name = row.get("name")
			if not isinstance(name, str) or not name or name in page or row.get("event_kind") != "Decision":
				raise ValueError("canonical unique commitment decision required")
			page.add(name)
			if name in found and found[name] != row:
				raise ValueError("commitment decision changed between reads")
			found[name] = row
			if len(found) > MAX_DECISIONS:
				raise ValueError("commitment discovery exceeds bound")

	for start in range(0, len(scopes), QUERY_CHUNK):
		chunk = scopes[start : start + QUERY_CHUNK]
		clauses = " OR ".join([condition] * len(chunk))
		merge(
			frappe.db.sql(
				f"SELECT {_columns(COMMITMENT_ROW_FIELDS)} FROM `tabHRP Budget Commitment Authorization` "  # nosec B608 - fixed columns/conditions
				f"WHERE ({clauses}) AND `event_kind`=%s ORDER BY `name` LIMIT %s FOR UPDATE",
				(*(v for scope in chunk for v in scope), "Decision", MAX_DECISIONS + 1),
				as_dict=True,
			)
		)
	for start in range(0, len(reservations), QUERY_CHUNK):
		chunk = reservations[start : start + QUERY_CHUNK]
		placeholders = ", ".join(["%s"] * len(chunk))
		merge(
			frappe.db.sql(
				f"SELECT {_columns(COMMITMENT_ROW_FIELDS)} FROM `tabHRP Budget Commitment Authorization` "  # nosec B608 - fixed columns/bound names
				f"WHERE `reservation` IN ({placeholders}) AND `event_kind`=%s ORDER BY `name` LIMIT %s FOR UPDATE",
				(*chunk, "Decision", MAX_DECISIONS + 1),
				as_dict=True,
			)
		)
	assert_budget_guards(ordered)
	return found


@dataclass(frozen=True, slots=True)
class ReservationActionEvidence:
	releases: dict = field(repr=False)
	commitments: dict = field(repr=False)


def _locked_pairs(kind, names, event_rows):
	table, fields, prefix = _spec(kind)
	expected = {
		name: {row["sequence"] - 1: row for row in event_rows[name][1:] if row["kind"] == kind}
		for name in names
	}
	count = sum(map(len, expected.values()))
	if count > MAX_DECISIONS:
		raise ValueError("independent evidence batch exceeds bound")
	placeholders = ", ".join(["%s"] * len(names))
	decisions = frappe.db.sql(
		f"SELECT {_columns(fields)} FROM `{table}` WHERE `reservation` IN ({placeholders}) "  # nosec B608 - closed _spec identifiers/bound names
		"AND `event_kind`=%s ORDER BY `reservation`, `previous_sequence`, `name` LIMIT %s FOR UPDATE",
		(*names, "Decision", count + 1),
		as_dict=True,
	)
	if len(decisions) != count:
		raise ValueError("all independent decisions must match physical event tails")
	by_root = {name: [] for name in names}
	confirmation_names = []
	used = set()
	decision_names = set()
	for row in decisions:
		parent, sequence = row.get("reservation"), row.get("previous_sequence")
		identity = (parent, sequence)
		event = expected.get(parent, {}).get(sequence)
		if (
			event is None
			or identity in used
			or row.get("name") in decision_names
			or row.get("event_kind") != "Decision"
			or event["source_action"] != prefix + row["name"]
			or event["previous_digest"] != row.get("previous_event_digest")
		):
			raise ValueError("independent decision differs from its exact event predecessor")
		used.add(identity)
		decision_names.add(row["name"])
		by_root[parent].append(row)
		confirmation_names.append(row.get("confirmation"))
	if (
		any(not isinstance(name, str) or not name for name in confirmation_names)
		or len(set(confirmation_names)) != count
	):
		raise ValueError("unique independent confirmations required")
	confirmed = {}
	for start in range(0, count, QUERY_CHUNK):
		chunk = confirmation_names[start : start + QUERY_CHUNK]
		placeholders = ", ".join(["%s"] * len(chunk))
		rows = frappe.db.sql(
			f"SELECT {_columns(fields)} FROM `{table}` WHERE `name` IN ({placeholders}) ORDER BY `name` FOR UPDATE",  # nosec B608 - closed _spec identifiers/bound names
			tuple(chunk),
			as_dict=True,
		)
		for row in rows:
			if row.get("name") in confirmed or row.get("event_kind") != "Confirmation":
				raise ValueError("canonical independent confirmation required")
			confirmed[row["name"]] = row
	if set(confirmed) != set(confirmation_names):
		raise ValueError("complete independent confirmations required")
	return {name: [(confirmed[row["confirmation"]], row) for row in rows] for name, rows in by_root.items()}


def locked_reservation_action_pairs(keys, positions, event_rows):
	"""Read both exact evidence sets; a zero expected count still checks for orphans.

	Caller supplies a complete guarded event batch and must validate every pair,
	current access and immutable storage. This grants no financial authority.
	"""
	ordered = ordered_budget_keys(keys)
	assert_budget_guards(ordered)
	require_release_evidence_storage()
	require_commitment_evidence_storage()
	if (
		not isinstance(positions, (list, tuple))
		or not 1 <= len(positions) <= QUERY_CHUNK
		or any(type(p) is not ReservationPosition for p in positions)
	):
		raise ValueError("bounded complete reservation positions required")
	names = tuple(p.reservation for p in positions)
	if len(set(names)) != len(names) or not isinstance(event_rows, dict) or set(event_rows) != set(names):
		raise ValueError("complete unique event parents required")
	for position in positions:
		rows = event_rows[position.reservation]
		if (
			not isinstance(rows, (list, tuple))
			or type(position.last_sequence) is not int
			or not 1 <= position.last_sequence <= MAX_RESERVATION_EVENTS
			or len(rows) != position.last_sequence
		):
			raise ValueError("complete bounded event history required")
		for sequence, row in enumerate(rows, 1):
			if (
				type(row.get("sequence")) is not int
				or row.get("sequence") != sequence
				or row.get("reservation") != position.reservation
				or row.get("kind")
				not in ({"Reserve"} if sequence == 1 else {"Release Reservation", "Commit"})
			):
				raise ValueError("ordered supported event history required")
	result = ReservationActionEvidence(
		_locked_pairs("Release Reservation", names, event_rows), _locked_pairs("Commit", names, event_rows)
	)
	assert_budget_guards(ordered)
	return result
