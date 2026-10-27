"""Check guarded initial reservation history against its durable decisions."""

from __future__ import annotations

from dataclasses import asdict

import frappe

from medops.common.authorized_reservation_projection import verify_authorized_initial_reservation
from medops.common.budget_control import ordered_budget_keys
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS
from medops.common.budget_reservation_events import ReservationPosition
from medops.common.budget_reservation_storage import ROW_FIELDS
from medops.common.reservation_authorization import AUTHORIZATION_ROW_FIELDS
from medops.hrp_budget.services.budget_usage_observation import MAX_USAGE_ROOTS
from medops.hrp_budget.services.control_guards import assert_material_guard_bindings, invalidate_budget_guards
from medops.hrp_budget.services.material_request_source import (
	MaterialRequestSource,
	locked_material_request_source,
)
from medops.services.errors import raise_ione_error

QUERY_CHUNK = 100


def _columns(fields):
	return ", ".join(f"`{name}`" for name in sorted(fields))


def _read_projections(positions):
	"""Read exact bounded row sets in deterministic chunks under the guard."""
	names = tuple(position.reservation for position in positions)
	root_fields = ANCHOR_ROW_FIELDS | {"source_snapshot_json"}
	for start in range(0, len(names), QUERY_CHUNK):
		chunk = names[start : start + QUERY_CHUNK]
		placeholders = ", ".join(["%s"] * len(chunk))
		roots = frappe.db.sql(
			f"SELECT {_columns(root_fields)} FROM `tabHRP Budget Reservation` "  # nosec B608 - fixed fields and placeholders
			f"WHERE `name` IN ({placeholders}) ORDER BY `name` FOR UPDATE",
			chunk,
			as_dict=True,
		)
		by_name = {row.name: row for row in roots}
		if len(roots) != len(chunk) or set(by_name) != set(chunk):
			raise ValueError("reservation root set differs")
		identities = tuple(by_name[name].source_identity_key for name in chunk)
		if any(not identity for identity in identities) or len(set(identities)) != len(chunk):
			raise ValueError("unique reservation source identities required")
		events = frappe.db.sql(
			f"SELECT {_columns(ROW_FIELDS)} FROM `tabHRP Budget Reservation Event` "  # nosec B608 - fixed fields and placeholders
			f"WHERE `reservation` IN ({placeholders}) ORDER BY `reservation`, `sequence` LIMIT %s FOR UPDATE",
			(*chunk, len(chunk) + 1),
			as_dict=True,
		)
		by_reservation = {row.reservation: row for row in events}
		if len(events) != len(chunk) or set(by_reservation) != set(chunk):
			raise ValueError("one initial event per reservation required")
		decisions = frappe.db.sql(
			f"SELECT {_columns(AUTHORIZATION_ROW_FIELDS)} FROM `tabHRP Budget Reservation Authorization` "  # nosec B608 - fixed fields and placeholders
			f"WHERE `reservation_identity` IN ({placeholders}) AND `event_kind`=%s "
			"ORDER BY `reservation_identity`, `name` LIMIT %s FOR UPDATE",
			(*identities, "Decision", len(chunk) + 1),
			as_dict=True,
		)
		by_identity = {row.reservation_identity: row for row in decisions}
		if len(decisions) != len(chunk) or set(by_identity) != set(identities):
			raise ValueError("one independent decision per reservation required")
		confirmation_names = tuple(by_identity[identity].confirmation for identity in identities)
		if any(not name for name in confirmation_names) or len(set(confirmation_names)) != len(chunk):
			raise ValueError("unique confirmation identities required")
		confirmations = frappe.db.sql(
			f"SELECT {_columns(AUTHORIZATION_ROW_FIELDS)} FROM `tabHRP Budget Reservation Authorization` "  # nosec B608 - fixed fields and placeholders
			f"WHERE `name` IN ({placeholders}) ORDER BY `name` FOR UPDATE",
			confirmation_names,
			as_dict=True,
		)
		by_confirmation = {row.name: row for row in confirmations}
		if len(confirmations) != len(chunk) or set(by_confirmation) != set(confirmation_names):
			raise ValueError("confirmation evidence set differs")
		for name in chunk:
			root = by_name[name]
			decision = by_identity[root.source_identity_key]
			yield root, [by_reservation[name]], by_confirmation[decision.confirmation], decision


def verify_guarded_initial_authorizations(keys, sources, positions):
	"""Fail closed on initial usage lacking a matching immutable decision.

	The caller supplies the exact source set returned by the guarded purchase
	inspection and complete usage positions. Later lifecycle events are not yet
	supported. No available balance or financial write authority is returned.
	"""
	actor = frappe.session.user
	try:
		ordered = ordered_budget_keys(keys)
		if not isinstance(sources, (tuple, list)) or any(
			type(source) is not MaterialRequestSource for source in sources
		):
			raise ValueError("guarded material sources required")
		if (
			not isinstance(positions, (tuple, list))
			or len(positions) > MAX_USAGE_ROOTS
			or any(type(position) is not ReservationPosition for position in positions)
		):
			raise ValueError("guarded reservation positions required")
		by_id = {(source.request, source.line, source.organization_unit): source for source in sources}
		if len(by_id) != len(sources) or len({position.reservation for position in positions}) != len(
			positions
		):
			raise ValueError("unique sources and positions required")
		bindings = tuple(sorted((*identity, source.digest()) for identity, source in by_id.items()))
		assert_material_guard_bindings(ordered, bindings)
		ordered_positions = tuple(sorted(positions, key=lambda value: value.reservation))
		used_sources = set()
		used_identities = set()
		used_decisions = set()
		used_confirmations = set()
		for position, (root, events, confirmation, decision) in zip(
			ordered_positions, _read_projections(ordered_positions), strict=True
		):
			if (
				root["source_identity_key"] in used_identities
				or decision["name"] in used_decisions
				or confirmation["name"] in used_confirmations
			):
				raise ValueError("authorization cannot be reused across reservations")
			used_identities.add(root["source_identity_key"])
			used_decisions.add(decision["name"])
			used_confirmations.add(confirmation["name"])
			identity = (root["source_name"], root["source_line"], root["organization_unit"])
			source = by_id.get(identity)
			if source is None:
				raise ValueError("authorized source is not locked or has changed")
			if identity not in used_sources and locked_material_request_source(*identity) != source:
				raise ValueError("authorized source is not locked or has changed")
			used_sources.add(identity)
			verified, _, _ = verify_authorized_initial_reservation(
				root, events, confirmation, decision, current_facts=asdict(source)
			)
			if verified != position or position.key not in ordered or frappe.session.user != actor:
				raise ValueError("authorized position differs from guarded usage")
		for identity in sorted(used_sources):
			if locked_material_request_source(*identity) != by_id[identity]:
				raise ValueError("authorized source changed during verification")
		assert_material_guard_bindings(ordered, bindings)
		if frappe.session.user != actor:
			raise_ione_error("CONFLICT")
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
