"""Reservation events admit only the private initial-write protocol."""

from __future__ import annotations

from medops.hrp_budget.services.initial_reservation_control import EVENT, require_initial_write
from medops.services.budget_documents import ControlledBudgetDocument
from medops.services.errors import raise_ione_error


class HRPBudgetReservationEvent(ControlledBudgetDocument):
	"""Deny normal writes; only the gated protocol can append the first event."""

	def before_insert(self):
		require_initial_write(self, doctype=EVENT, stage="insert", named=False)

	def validate(self):
		require_initial_write(self, doctype=EVENT, stage="insert")

	def db_insert(self, *args, **kwargs):
		state = require_initial_write(self, doctype=EVENT, stage="insert")
		result = super().db_insert(*args, **kwargs)
		state.event_inserted = True
		return result

	def db_update(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_set(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")
