"""Exact private Confirmation or paired Decision/Commit append; default disabled."""

from __future__ import annotations

from medops.hrp_budget.services.reservation_commitment_confirmation import (
	require_commitment_confirmation_write,
)
from medops.hrp_budget.services.reservation_commitment_control import (
	commitment_post_active,
	require_commitment_post_write,
)
from medops.services.budget_documents import ControlledBudgetDocument
from medops.services.errors import raise_ione_error


class HRPBudgetCommitmentAuthorization(ControlledBudgetDocument):
	def _require_append(self, *, named=True):
		if commitment_post_active():
			return require_commitment_post_write(self, named=named)
		return require_commitment_confirmation_write(self, named=named)

	def before_insert(self):
		self._require_append(named=False)

	def validate(self):
		self._require_append()

	def db_insert(self, *args, **kwargs):
		posting = commitment_post_active()
		state = self._require_append()
		result = super().db_insert(*args, **kwargs)
		if posting:
			state.evidence_inserted = True
		else:
			state.inserted = True
		return result

	def db_update(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_set(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")
