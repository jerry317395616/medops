"""Append exact release evidence only through the default-disabled private gate."""

from __future__ import annotations

from medops.hrp_budget.services.reservation_release_control import require_reservation_release_write
from medops.services.budget_documents import ControlledBudgetDocument
from medops.services.errors import raise_ione_error


class HRPBudgetReservationRelease(ControlledBudgetDocument):
	def before_insert(self):
		require_reservation_release_write(self, named=False)

	def validate(self):
		require_reservation_release_write(self)

	def db_insert(self, *args, **kwargs):
		state = require_reservation_release_write(self)
		result = super().db_insert(*args, **kwargs)
		state.evidence_inserted = True
		return result

	def db_update(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_set(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")
