"""Private guarded lookup of a complete supplied reservation renewal lineage.

This reader does not authorize renewal or posting. Its caller must own the
transaction and hold the exact material source and budget guards.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import frappe

from medops.common.material_source_snapshot import material_source_snapshot
from medops.common.reservation_authorization import (
	AUTHORIZATION_ROW_FIELDS,
	ReservationConfirmation,
	ReservationDecision,
	ReservationIntent,
	verify_reservation_authorization_rows,
	verify_reservation_confirmation_row,
)
from medops.common.reservation_renewal import (
	MAX_RENEWALS,
	RENEWAL_ROW_FIELDS,
	ReservationRenewalDecision,
	verify_reservation_renewal_rows,
)
from medops.hrp_budget.services.approval import session_digest
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.material_budget_guards import assert_material_budget_guards
from medops.hrp_budget.services.material_request_source import (
	MaterialRequestSource,
	locked_material_request_source,
)
from medops.hrp_budget.services.reservation_confirmation import require_reservation_actor_scope
from medops.services.errors import raise_ione_error


@dataclass(frozen=True, slots=True)
class LockedRenewalLineage:
	confirmation: ReservationConfirmation
	original: ReservationDecision
	renewals: tuple[tuple[ReservationConfirmation, ReservationRenewalDecision], ...]


def _evidence_rows(names):
	if not names or len(names) > MAX_RENEWALS or len(set(names)) != len(names):
		raise ValueError("bounded distinct authorization evidence names required")
	fields = ", ".join(f"`{field}`" for field in sorted(AUTHORIZATION_ROW_FIELDS))
	placeholders = ", ".join(["%s"] * len(names))
	rows = frappe.db.sql(
		f"SELECT {fields} FROM `tabHRP Budget Reservation Authorization` "  # nosec B608 - fixed fields and placeholders
		f"WHERE `name` IN ({placeholders}) ORDER BY `name` FOR UPDATE",
		tuple(names),
		as_dict=True,
	)
	by_name = {row["name"]: row for row in rows}
	if len(rows) != len(names) or set(by_name) != set(names):
		raise ValueError("authorization evidence set differs")
	return by_name


def _check_source(item, source, actor, session):
	require_reservation_actor_scope(item, actor, session)
	assert_material_budget_guards(source, [item.key])
	current = locked_material_request_source(source.request, source.line, source.organization_unit)
	snapshot, digest = material_source_snapshot(asdict(current))
	if current != source or snapshot != item.source_snapshot or digest != item.source_snapshot_digest:
		raise ValueError("material source differs from confirmation")
	assert_material_budget_guards(source, [item.key])
	require_reservation_actor_scope(item, actor, session)


def locked_renewal_lineage(item, source, confirmation_name, decision_name):
	"""Read all renewal rows for one identity under its already-held guard.

	This is only a locked observation. Complete cooperating writers and protected
	storage are prerequisites; it must not be used as a posting authorization.
	"""
	try:
		if type(item) is not ReservationIntent or type(source) is not MaterialRequestSource:
			raise ValueError("canonical intent and locked material source required")
		actor = frappe.session.user
		session = session_digest("budget-reservation:" + item.reservation_identity)
		_check_source(item, source, actor, session)
		original_rows = _evidence_rows((confirmation_name, decision_name))
		first = original_rows[confirmation_name]
		second = original_rows[decision_name]
		checked, original = verify_reservation_authorization_rows(first, second)
		confirmed_item, confirmation = verify_reservation_confirmation_row(first)
		if checked != item or confirmed_item != item:
			raise ValueError("original authorization differs from current intent")
		fields = ", ".join(f"`{field}`" for field in sorted(RENEWAL_ROW_FIELDS))
		rows = frappe.db.sql(
			f"SELECT {fields} FROM `tabHRP Budget Reservation Renewal` "  # nosec B608 - fixed fields and placeholders
			"WHERE `reservation_identity`=%s ORDER BY `sequence`, `name` LIMIT %s FOR UPDATE",
			(item.reservation_identity, MAX_RENEWALS + 1),
			as_dict=True,
		)
		if len(rows) > MAX_RENEWALS:
			raise ValueError("renewal history exceeds safe bound")
		names = tuple(row["confirmation"] for row in rows)
		confirmations = _evidence_rows(names) if names else {}
		values = []
		for row in rows:
			stored_item, new_confirmation = verify_reservation_confirmation_row(
				confirmations[row["confirmation"]]
			)
			if stored_item != item:
				raise ValueError("renewal confirmation differs from original intent")
			values.append((new_confirmation, row))
		pairs = verify_reservation_renewal_rows(item, confirmation, original, values)
		_check_source(item, source, actor, session)
		return LockedRenewalLineage(confirmation, original, pairs)
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
