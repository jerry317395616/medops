"""Resolve budget lock references without granting write authority."""

from __future__ import annotations

from dataclasses import asdict

from medops.common.budget_control import BudgetControlKey, budget_control_key, ordered_budget_keys
from medops.common.budget_control_release import budget_keys_for_release_batch
from medops.common.budget_ledger import BudgetReleaseFacts
from medops.common.budget_planning import FUND, VALUE
from medops.hrp_budget.services.planning import native_document

# Explicit mapping only. Never accept a caller-selected DocType here.
_KEY_REFERENCES = (
	("company", "Company"),
	("hospital", "HRP Hospital"),
	("organization_unit", "HRP Organization Unit"),
	("fiscal_year", "Fiscal Year"),
	("budget_subject", VALUE),
	("funding_source", FUND),
	("project", "Project"),
)


def resolve_budget_control_key(value: object) -> BudgetControlKey:
	"""Read actual names before generating a lock key.

	The standard reader enforces current document/field read access; the returned
	key alone does not prove source relationships, effectiveness, write scope,
	complete events, or locks. A writer must revalidate all of those under its
	transaction guard. No caller-chosen table or permission bypass is allowed.
	"""
	return _resolve_key(budget_control_key(value))


def _resolve_key(key: BudgetControlKey, *, cache=None) -> BudgetControlKey:
	values = asdict(key)
	for field, doctype in _KEY_REFERENCES:
		name = values[field]
		if name is not None:
			if cache is None:
				doc = native_document(doctype, name, {"name"})
			else:
				doc = native_document(doctype, name, {"name"}, cache=cache)
			values[field] = doc.name
	return budget_control_key(values)


def resolve_release_control_keys(
	facts: BudgetReleaseFacts, *, original: BudgetReleaseFacts | None = None
) -> tuple[BudgetControlKey, ...]:
	"""Derive and resolve a whole release batch; this grants no write authority.

	The cache lives only for this call. The eventual transactional writer must
	refresh all permissions and source facts under the budget guard, and reject
	changes before writing. Canonical aliases are deduplicated after resolution.
	"""
	keys = budget_keys_for_release_batch(facts, original=original)
	cache = {}
	return ordered_budget_keys([_resolve_key(key, cache=cache) for key in keys])
