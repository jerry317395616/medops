"""Inspect reservation chains across guarded budget scopes; no spending authority."""

from __future__ import annotations

from collections.abc import Mapping

import frappe

from medops.common.budget_control import KEY_FIELDS, BudgetControlKey, budget_control_key, ordered_budget_keys
from medops.common.budget_ledger import LEDGER, LEDGER_PUBLIC_FIELDS
from medops.common.budget_reservation_anchor import (
	ANCHOR_ROW_FIELDS,
	verify_frozen_reservation_anchor_row,
	verify_reservation_anchor_row,
)
from medops.common.budget_reservation_events import ReservationPosition
from medops.common.budget_reservation_proof import ReservationSource
from medops.common.budget_reservation_storage import ROW_FIELDS
from medops.common.reservation_commitment_history import COMMIT_ACTION_PREFIX
from medops.common.reservation_release import RELEASE_ACTION_PREFIX
from medops.hrp_budget.services.control_guards import assert_budget_guards, invalidate_budget_guards
from medops.hrp_budget.services.planning_access import READ_ROLES
from medops.hrp_budget.services.release_usage_guard import require_valid_usage_storage
from medops.hrp_budget.services.reservation_action_observation import guarded_commitment_decision_inventory
from medops.hrp_budget.services.reservation_release_observation import guarded_release_decision_inventory
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.data_access import require_scope
from medops.services.errors import raise_ione_error

MAX_USAGE_ROOTS = 10000
MAX_USAGE_EVENTS = 100000
QUERY_CHUNK = 100


def _chunks(values):
	for start in range(0, len(values), QUERY_CHUNK):
		yield values[start : start + QUERY_CHUNK]


def usage_scope(key: BudgetControlKey) -> tuple[str, str, str, str, str, str]:
	return (
		key.company,
		key.hospital,
		key.organization_unit,
		key.budget_subject,
		key.funding_source,
		key.project or "",
	)


def _fields(fields: frozenset[str]) -> str:
	return ", ".join(f"`{name}`" for name in sorted(fields))


def check_usage_access(units, actor):
	from medops.hrp_budget.services.release_execution import refresh_financial_authority

	if frappe.session.user != actor:
		raise_ione_error("CONFLICT")
	refresh_financial_authority(actor)
	user = frappe.db.get_value("User", actor, ["enabled", "user_type"], as_dict=True)
	if (
		not user
		or not user.enabled
		or user.user_type != "System User"
		or not READ_ROLES.intersection(frappe.get_roles(actor))
		or not frappe.has_permission(LEDGER, "read", user=actor)
	):
		raise_ione_error("PERMISSION_DENIED")
	for company, hospital, unit in units:
		require_scope(LEDGER, company=company, hospital=hospital, organization_unit=unit)
		probe = frappe.get_doc(
			dict(doctype=LEDGER, company=company, hospital=hospital, organization_unit=unit)
		)
		if not can_read_named_fields(probe, set(LEDGER_PUBLIC_FIELDS), user=actor):
			raise_ione_error("PERMISSION_DENIED")


def _merge_rows(target, rows, limit):
	if len(rows) > limit:
		raise ValueError("usage observation exceeds bound")
	page_names = set()
	for row in rows:
		name = row.get("name")
		if not isinstance(name, str) or not name or name in page_names:
			raise ValueError("invalid usage record identity")
		page_names.add(name)
		if name in target and target[name] != row:
			raise ValueError("usage record changed between reads")
		target[name] = row
		if len(target) > limit:
			raise ValueError("usage observation exceeds bound")


def query_usage_scope(table, fields, scopes, limit):
	if (table, fields) not in {
		("tabHRP Budget Reservation", ANCHOR_ROW_FIELDS),
		("tabHRP Budget Reservation Event", ROW_FIELDS),
	}:
		raise ValueError("unsupported usage table")
	rows = {}
	condition = """(`company`=%s AND `hospital`=%s AND `organization_unit`=%s
		AND `budget_subject`=%s AND `funding_source`=%s AND COALESCE(`project`, '')=%s)"""
	for chunk in _chunks(scopes):
		clauses = " OR ".join([condition] * len(chunk))
		parameters = (*(value for scope in chunk for value in scope), limit + 1)
		found = frappe.db.sql(
			f"SELECT {_fields(fields)} FROM `{table}` WHERE {clauses} ORDER BY `name` LIMIT %s",  # nosec B608 - fixed identifiers and bound values
			parameters,
			as_dict=True,
		)
		_merge_rows(rows, found, limit)
	return rows


def _parent_events(names, target):
	for chunk in _chunks(names):
		placeholders = ", ".join(["%s"] * len(chunk))
		found = frappe.db.sql(
			f"""SELECT {_fields(ROW_FIELDS)} FROM `tabHRP Budget Reservation Event`
			WHERE `reservation` IN ({placeholders}) ORDER BY `name` LIMIT %s""",  # nosec B608 - fixed identifiers and bound values
			(*chunk, MAX_USAGE_EVENTS + 1),
			as_dict=True,
		)
		_merge_rows(target, found, MAX_USAGE_EVENTS)


def guarded_budget_usage_observation(value: object) -> tuple[ReservationPosition, ...]:
	"""Check bounded physical chains for every guarded scope, including orphans.

	This is internal evidence only. It does not prove that an aggregate was
	authorized, that its source snapshot is authentic, or that a budget is
	available. Before enabling any writer, all usage writers must share the guard,
	initial storage and schema must be verified, and source, actor, approval and
	complete allocation policies must be independently authenticated. The caller
	must roll back the whole transaction on any failure.
	"""
	actor = frappe.session.user
	try:
		keys = ordered_budget_keys(value)
		assert_budget_guards(keys)
		scopes = sorted({usage_scope(key) for key in keys})
		units = sorted({(key.company, key.hospital, key.organization_unit) for key in keys})
		check_usage_access(units, actor)
		require_valid_usage_storage(keys)
		roots = query_usage_scope("tabHRP Budget Reservation", ANCHOR_ROW_FIELDS, scopes, MAX_USAGE_ROOTS)
		events = query_usage_scope("tabHRP Budget Reservation Event", ROW_FIELDS, scopes, MAX_USAGE_EVENTS)
		_parent_events(tuple(sorted(roots)), events)
		release_decisions = guarded_release_decision_inventory(keys, tuple(sorted(roots)))
		commitment_decisions = guarded_commitment_decision_inventory(keys, tuple(sorted(roots)))
		decisions_by_root = {name: {} for name in roots}
		for prefix, kind, inventory in (
			(RELEASE_ACTION_PREFIX, "Release Reservation", release_decisions),
			(COMMIT_ACTION_PREFIX, "Commit", commitment_decisions),
		):
			for name, decision in inventory.items():
				parent = decision["reservation"]
				if parent not in roots:
					raise ValueError("orphan independent decision in guarded budget scope")
				key = budget_control_key({field: decision[field] for field in KEY_FIELDS})
				root_key = budget_control_key({field: roots[parent][field] for field in KEY_FIELDS})
				if key != root_key or decision["budget_key_digest"] != key.identity:
					raise ValueError("independent decision scope differs from its reservation")
				decisions_by_root[parent][prefix + name] = (decision, kind)
		groups: dict[str, list[Mapping]] = {name: [] for name in roots}
		actions = set()
		sequences = set()
		digests = set()
		for row in events.values():
			parent = row.get("reservation")
			if parent not in groups:
				raise ValueError("orphan usage event in guarded scope")
			for field, seen in (
				("source_action_key", actions),
				("sequence_key", sequences),
				("event_digest", digests),
			):
				identity = row.get(field)
				if not isinstance(identity, str) or not identity or identity in seen:
					raise ValueError("duplicate or invalid event identity")
				seen.add(identity)
			groups[parent].append(row)
		positions = []
		source_keys = set()
		for name, row in roots.items():
			if row.get("source_doctype") != "Material Request" or row.get("source_revision") != row.get(
				"source_snapshot_digest"
			):
				raise ValueError("unsupported source or revision")
			source = ReservationSource(
				row["source_doctype"],
				row["source_name"],
				row["source_revision"],
				row["source_line"],
				row["source_allocation"],
				"Reserve",
			)
			identity = row.get("source_identity_key")
			if identity in source_keys:
				raise ValueError("duplicate source allocation")
			source_keys.add(identity)
			chain = groups[name]
			if any(
				(
					event.get("source_doctype"),
					event.get("source_name"),
					event.get("source_revision"),
					event.get("source_line"),
					event.get("source_allocation"),
				)
				!= (source.doctype, source.name, source.revision, source.line, source.allocation)
				for event in chain
			):
				raise ValueError("unsupported event source")
			chain = sorted(chain, key=lambda event: event["sequence"])
			frozen = row.get("event_count") == 1
			verifier = verify_frozen_reservation_anchor_row if frozen else verify_reservation_anchor_row
			position = verifier(
				row,
				chain,
				expected_source=source,
				source_snapshot_digest=row["source_snapshot_digest"],
				cost_center=row["cost_center"],
				**({"allow_commitments": True} if frozen else {}),
			)
			decisions = decisions_by_root[name]
			if decisions and not frozen:
				raise ValueError("release decisions require an immutable initial root")
			if frozen:
				if len(decisions) != len(chain) - 1:
					raise ValueError("physical release decisions and tail events differ")
				for event in chain[1:]:
					bound = decisions.get(event["source_action"])
					if bound is None:
						raise ValueError("independent event decision is missing")
					decision, kind = bound
					if (
						kind != event["kind"]
						or decision["previous_sequence"] != event["sequence"] - 1
						or decision["previous_event_digest"] != event["previous_digest"]
					):
						raise ValueError("release event predecessor differs from its decision")
			if position.reservation != name or usage_scope(position.key) not in scopes:
				raise ValueError("usage scope differs from aggregate")
			positions.append(position)
		check_usage_access(units, actor)
		require_valid_usage_storage(keys)
		assert_budget_guards(keys)
		return tuple(sorted(positions, key=lambda item: item.reservation))
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
