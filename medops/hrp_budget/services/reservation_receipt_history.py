"""Bounded history re-read for private initial reservation receipt verification."""

from __future__ import annotations

import frappe

from medops.common.budget_reservation_events import MAX_RESERVATION_EVENTS, ReservationPosition
from medops.common.budget_reservation_proof import verify_reservation_records
from medops.common.budget_reservation_storage import ROW_FIELDS, reservation_record_from_row
from medops.hrp_budget.services.material_budget_guards import assert_material_budget_guards
from medops.hrp_budget.services.reservation_release_observation import locked_release_pairs


def read_locked_initial_receipt_history(item, source, reservation, event, *, observed_position=None):
	"""Re-read a previously inspected chain without granting any write authority.

	Only the caller's fresh complete purchase inspection may supply a current
	position. Without that inspection, the existing guard path remains limited
	to the single event just posted. Callers must independently authenticate the
	frozen root, original/renewed decision and every returned release pair, then
	recheck current actor/source rights. Any error requires full rollback.
	"""
	assert_material_budget_guards(source, [item.key])
	count = 1
	if observed_position is not None:
		if (
			type(observed_position) is not ReservationPosition
			or observed_position.reservation != reservation
			or observed_position.key != item.key
			or observed_position.currency != item.currency
			or observed_position.precision != item.precision
			or type(observed_position.last_sequence) is not int
			or not 1 <= observed_position.last_sequence <= MAX_RESERVATION_EVENTS
		):
			raise ValueError("complete inspected receipt position required")
		count = observed_position.last_sequence
	columns = ", ".join(f"`{field}`" for field in sorted(ROW_FIELDS))
	rows = frappe.db.sql(
		f"SELECT {columns} FROM `tabHRP Budget Reservation Event` "  # nosec B608 - fixed fields and bound values
		"WHERE `reservation`=%s ORDER BY `sequence`, `name` LIMIT %s FOR UPDATE",
		(reservation, count + 1),
		as_dict=True,
	)
	if len(rows) != count or rows[0]["name"] != event:
		raise ValueError("complete receipt history and original event required")
	position = verify_reservation_records([reservation_record_from_row(row) for row in rows])
	if position.reservation != reservation or (
		observed_position is not None and position != observed_position
	):
		raise ValueError("receipt history changed after full purchase inspection")
	pairs = locked_release_pairs([item.key], (position,))[reservation]
	assert_material_budget_guards(source, [item.key])
	return rows, pairs
