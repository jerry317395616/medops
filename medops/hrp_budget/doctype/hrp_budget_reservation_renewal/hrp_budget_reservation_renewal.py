"""Append only one transaction-bound renewal; standard writes remain closed."""

from __future__ import annotations

from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.reservation_renewal_control import require_renewal_write
from medops.services.budget_documents import ControlledBudgetDocument
from medops.services.errors import raise_ione_error


def _deny():
	invalidate_budget_guards()
	raise_ione_error("OPERATION_NOT_ALLOWED")


class HRPBudgetReservationRenewal(ControlledBudgetDocument):
	def before_insert(self):
		require_renewal_write(self, named=False)

	def validate(self):
		require_renewal_write(self)

	def db_insert(self, *args, **kwargs):
		state = require_renewal_write(self)
		try:
			result = super().db_insert(*args, **kwargs)
			state.inserted = True
			return result
		except BaseException:
			invalidate_budget_guards()
			raise

	def db_update(self, *args, **kwargs):
		_deny()

	def db_set(self, *args, **kwargs):
		_deny()

	def on_trash(self):
		_deny()

	def before_rename(self, *args, **kwargs):
		_deny()
