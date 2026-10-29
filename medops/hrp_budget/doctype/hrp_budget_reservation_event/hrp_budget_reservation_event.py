"""Reservation events admit exact private initial, release or commitment protocols."""

from __future__ import annotations

from medops.hrp_budget.services.initial_reservation_control import EVENT, require_initial_write
from medops.hrp_budget.services.reservation_commitment_control import (
	commitment_post_active,
	require_commitment_post_write,
)
from medops.hrp_budget.services.reservation_release_control import (
	release_write_active,
	require_reservation_release_write,
)
from medops.services.budget_documents import ControlledBudgetDocument
from medops.services.errors import raise_ione_error


class HRPBudgetReservationEvent(ControlledBudgetDocument):
	"""Deny normal writes; dispatch only an explicitly active private gate."""

	def _require_append(self, *, named=True):
		if commitment_post_active():
			return require_commitment_post_write(self, doctype=EVENT, named=named)
		if release_write_active():
			return require_reservation_release_write(self, doctype=EVENT, named=named)
		return require_initial_write(self, doctype=EVENT, stage="insert", named=named)

	def before_insert(self):
		self._require_append(named=False)

	def validate(self):
		self._require_append()

	def db_insert(self, *args, **kwargs):
		state = self._require_append()
		result = super().db_insert(*args, **kwargs)
		state.event_inserted = True
		return result

	def db_update(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_set(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")
