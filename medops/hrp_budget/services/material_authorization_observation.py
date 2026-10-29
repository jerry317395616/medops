"""Check guarded initial and released history against independent decisions."""

from __future__ import annotations

from dataclasses import asdict

import frappe

from medops.common.authorized_reservation_projection import (
	verify_authorized_released_reservation,
)
from medops.common.budget_control import ordered_budget_keys
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS
from medops.common.budget_reservation_events import MAX_RESERVATION_EVENTS, ReservationPosition
from medops.common.budget_reservation_storage import ROW_FIELDS
from medops.common.reservation_authorization import (
	AUTHORIZATION_ROW_FIELDS,
	verify_reservation_authorization_rows,
	verify_reservation_confirmation_row,
)
from medops.common.reservation_renewal import (
	MAX_RENEWALS,
	RENEWAL_ROW_FIELDS,
	verify_reservation_renewal_rows,
)
from medops.hrp_budget.services import budget_usage_observation as access
from medops.hrp_budget.services.budget_usage_observation import MAX_USAGE_EVENTS, MAX_USAGE_ROOTS
from medops.hrp_budget.services.control_guards import assert_material_guard_bindings, invalidate_budget_guards
from medops.hrp_budget.services.material_request_source import (
	MaterialRequestSource,
	locked_material_request_source,
)
from medops.hrp_budget.services.reservation_action_observation import locked_reservation_action_pairs
from medops.services.errors import raise_ione_error

QUERY_CHUNK = 100


def _columns(fields):
	return ", ".join(f"`{name}`" for name in sorted(fields))


def _read_renewals(identities):
	"""Fetch all bounded renewal rows and confirmations under the existing guard."""
	indexes = frappe.db.sql(
		"""SELECT 1 FROM information_schema.STATISTICS
		WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s AND SEQ_IN_INDEX=1 LIMIT 1""",
		("tabHRP Budget Reservation Renewal", "reservation_identity"),
	)
	if not indexes:
		raise_ione_error("CONFIGURATION_INVALID")
	placeholders = ", ".join(["%s"] * len(identities))
	rows = frappe.db.sql(
		f"SELECT {_columns(RENEWAL_ROW_FIELDS)} FROM `tabHRP Budget Reservation Renewal` "  # nosec B608 - fixed fields and placeholders
		f"WHERE `reservation_identity` IN ({placeholders}) "
		"ORDER BY `reservation_identity`, `sequence`, `name` LIMIT %s FOR UPDATE",
		(*identities, len(identities) * MAX_RENEWALS + 1),
		as_dict=True,
	)
	if len(rows) > len(identities) * MAX_RENEWALS:
		raise ValueError("renewal history exceeds bounded read")
	grouped = {identity: [] for identity in identities}
	names = []
	for row in rows:
		identity, name = row["reservation_identity"], row["confirmation"]
		if identity not in grouped or not isinstance(name, str) or not name:
			raise ValueError("invalid renewal identity or confirmation")
		grouped[identity].append(row)
		if len(grouped[identity]) > MAX_RENEWALS:
			raise ValueError("renewal history exceeds per-reservation limit")
		names.append(name)
	if len(set(names)) != len(names):
		raise ValueError("renewal confirmation cannot be reused")
	confirmations = {}
	ordered_names = sorted(names)
	for start in range(0, len(ordered_names), QUERY_CHUNK):
		chunk = ordered_names[start : start + QUERY_CHUNK]
		placeholders = ", ".join(["%s"] * len(chunk))
		found = frappe.db.sql(
			f"SELECT {_columns(AUTHORIZATION_ROW_FIELDS)} FROM `tabHRP Budget Reservation Authorization` "  # nosec B608 - fixed fields and placeholders
			f"WHERE `name` IN ({placeholders}) ORDER BY `name` FOR UPDATE",
			tuple(chunk),
			as_dict=True,
		)
		for row in found:
			if row["name"] in confirmations:
				raise ValueError("duplicate renewal confirmation")
			confirmations[row["name"]] = row
	if set(confirmations) != set(names):
		raise ValueError("renewal confirmation evidence set differs")
	return grouped, confirmations


def _read_projections(positions, keys):
	"""Read exact bounded row sets in deterministic chunks under the guard."""
	names = tuple(position.reservation for position in positions)
	by_position = {position.reservation: position for position in positions}
	root_fields = ANCHOR_ROW_FIELDS | {"source_snapshot_json"}
	for start in range(0, len(names), QUERY_CHUNK):
		chunk = names[start : start + QUERY_CHUNK]
		chunk_positions = tuple(by_position[name] for name in chunk)
		expected_events = sum(position.last_sequence for position in chunk_positions)
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
			(*chunk, expected_events + 1),
			as_dict=True,
		)
		by_reservation = {name: [] for name in chunk}
		for row in events:
			if row.reservation not in by_reservation:
				raise ValueError("event parent differs from requested roots")
			by_reservation[row.reservation].append(row)
		if len(events) != expected_events or any(
			len(by_reservation[position.reservation]) != position.last_sequence
			for position in chunk_positions
		):
			raise ValueError("complete physical event history differs from guarded positions")
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
		renewals, renewal_confirmations = _read_renewals(identities)
		actions = locked_reservation_action_pairs(keys, chunk_positions, by_reservation)
		for name in chunk:
			root = by_name[name]
			decision = by_identity[root.source_identity_key]
			yield (
				root,
				by_reservation[name],
				by_confirmation[decision.confirmation],
				decision,
				renewals[root.source_identity_key],
				renewal_confirmations,
				actions.releases[name],
				actions.commitments[name],
			)


def verify_guarded_initial_authorizations(keys, sources, positions):
	"""Fail closed on usage lacking complete initial and release decisions.

	The caller supplies the exact source set returned by the guarded purchase
	inspection and complete usage positions. Only initial Reserve and independently
	authorized Release Reservation tails are supported. This grants no write.
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
			or any(
				type(position.last_sequence) is not int
				or not 1 <= position.last_sequence <= MAX_RESERVATION_EVENTS
				for position in positions
			)
			or sum(position.last_sequence for position in positions) > MAX_USAGE_EVENTS
		):
			raise ValueError("guarded reservation positions required")
		by_id = {(source.request, source.line, source.organization_unit): source for source in sources}
		if len(by_id) != len(sources) or len({position.reservation for position in positions}) != len(
			positions
		):
			raise ValueError("unique sources and positions required")
		bindings = tuple(sorted((*identity, source.digest()) for identity, source in by_id.items()))
		assert_material_guard_bindings(ordered, bindings)
		units = sorted({(key.company, key.hospital, key.organization_unit) for key in ordered})
		access.check_usage_access(units, actor)
		ordered_positions = tuple(sorted(positions, key=lambda value: value.reservation))
		used_sources = set()
		used_identities = set()
		used_decisions = set()
		used_confirmations = set()
		positions_by_source = {identity: [] for identity in by_id}
		for position, (
			root,
			events,
			confirmation,
			decision,
			renewal_rows,
			renewal_confirmations,
			releases,
			commitments,
		) in zip(ordered_positions, _read_projections(ordered_positions, ordered), strict=True):
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
			pairs = ()
			if events[0]["proof_version"] == 1:
				if renewal_rows:
					raise ValueError("original event cannot have a renewal successor")
			else:
				item, original = verify_reservation_authorization_rows(confirmation, decision)
				checked, first = verify_reservation_confirmation_row(confirmation)
				if checked != item:
					raise ValueError("original confirmation differs from decision")
				values = []
				for row in renewal_rows:
					new_item, new_confirmation = verify_reservation_confirmation_row(
						renewal_confirmations[row["confirmation"]]
					)
					if new_item != item:
						raise ValueError("renewal confirmation differs from original intent")
					values.append((new_confirmation, row))
				pairs = verify_reservation_renewal_rows(item, first, original, values)
			verified, _, _ = verify_authorized_released_reservation(
				root,
				events,
				confirmation,
				decision,
				releases,
				current_facts=asdict(source),
				renewals=pairs,
				commitments=commitments,
			)
			if verified != position or position.key not in ordered or frappe.session.user != actor:
				raise ValueError("authorized position differs from guarded usage")
			positions_by_source[identity].append(position)
		for identity in sorted(used_sources):
			if locked_material_request_source(*identity) != by_id[identity]:
				raise ValueError("authorized source changed during verification")
		access.check_usage_access(units, actor)
		assert_material_guard_bindings(ordered, bindings)
		if frappe.session.user != actor:
			raise_ione_error("CONFLICT")
		return {identity: tuple(positions) for identity, positions in positions_by_source.items()}
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
