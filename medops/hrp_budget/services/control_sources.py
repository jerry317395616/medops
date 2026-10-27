"""Resolve budget lock references without granting write authority."""

from __future__ import annotations

from dataclasses import asdict

from medops.common.budget_control import BudgetControlKey, budget_control_key
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
	key = budget_control_key(value)
	values = asdict(key)
	for field, doctype in _KEY_REFERENCES:
		name = values[field]
		if name is not None:
			doc = native_document(doctype, name, {"name"})
			values[field] = doc.name
	return budget_control_key(values)
