"""Append only an exact private Confirmation; Decision/financial writes stay closed."""

from __future__ import annotations

from medops.hrp_budget.services.reservation_commitment_confirmation import (
	require_commitment_confirmation_write,
)
from medops.services.budget_documents import ControlledBudgetDocument
from medops.services.errors import raise_ione_error


class HRPBudgetCommitmentAuthorization(ControlledBudgetDocument):
	def before_insert(self):
		require_commitment_confirmation_write(self, named=False)

	def validate(self):
		require_commitment_confirmation_write(self)

	def db_insert(self, *args, **kwargs):
		state = require_commitment_confirmation_write(self)
		result = super().db_insert(*args, **kwargs)
		state.inserted = True
		return result

	def db_update(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_set(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")
