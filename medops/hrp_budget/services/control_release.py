"""Preliminary guarded resolution of release keys; no authority to post."""

from __future__ import annotations

from medops.common.budget_ledger import BudgetReleaseFacts
from medops.hrp_budget.services.control_guards import (
	acquire_budget_guards,
	assert_budget_guards,
	invalidate_budget_guards,
)
from medops.hrp_budget.services.control_sources import resolve_release_control_keys
from medops.services.errors import raise_ione_error


def prepare_release_guards(facts: BudgetReleaseFacts, *, original: BudgetReleaseFacts | None = None):
	"""Resolve, lock and re-resolve names without granting financial authority.

	The caller still must prove persisted source completeness and relationships,
	current write scope, currency, all events, and the whole-batch authorization
	under the same guard. Any later failure must abort the whole transaction.
	Deadlocks and timeouts escape unchanged for the outer whole-transaction retry.
	"""
	keys = resolve_release_control_keys(facts, original=original)
	acquire_budget_guards(keys)
	try:
		current = resolve_release_control_keys(facts, original=original)
		assert_budget_guards(keys)
		if current != keys:
			raise_ione_error("CONFLICT")
	except BaseException:
		# This changes no database state, so deadlocks and timeouts retain their
		# original signal. The flag survives rollback to later savepoints.
		invalidate_budget_guards()
		raise
	return keys
