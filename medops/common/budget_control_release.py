"""Derive control keys from sealed release facts without granting authority."""

from __future__ import annotations

from medops.common.budget_control import (
	MAX_CONTROL_KEYS,
	BudgetControlKey,
	budget_control_key,
	ordered_budget_keys,
)
from medops.common.budget_ledger import BudgetReleaseFacts, release_facts, reversal_facts


def budget_keys_for_release_batch(
	facts: BudgetReleaseFacts, *, original: BudgetReleaseFacts | None = None
) -> tuple[BudgetControlKey, ...]:
	"""Derive original-leaf keys for a release or its exact full reversal.

	This checks the immutable in-memory intent only; it cannot prove the persisted
	batch is complete, the references are canonical in the current database, the
	actor is authorized, or the transaction holds any locks. Those checks belong
	to the future writer. Too many source lines fail closed, without truncation.
	"""
	if type(facts) is not BudgetReleaseFacts:
		raise ValueError("budget release facts required")
	if facts.operation == "Release" and original is None:
		source = facts
	elif facts.operation == "Reverse" and type(original) is BudgetReleaseFacts:
		source = original
	else:
		raise ValueError("original release required for full reversal")
	if len(source.snapshot.lines) > MAX_CONTROL_KEYS:
		raise ValueError("budget control batch exceeds key limit")
	if facts.operation == "Reverse":
		if source.operation != "Release" or facts != reversal_facts(
			source, posting_date=facts.posting_date, reason=facts.reason, prepared_by=facts.prepared_by
		):
			raise ValueError("full reversal must match its original release")
	elif source != release_facts(
		source.snapshot,
		source.approval,
		posting_date=source.posting_date,
		reason=source.reason,
		prepared_by=source.prepared_by,
	):
		raise ValueError("release intent differs from sealed source")
	context = source.snapshot.context
	return ordered_budget_keys(
		[
			budget_control_key(
				{
					"company": context.company,
					"hospital": context.hospital,
					"organization_unit": line.organization_unit,
					"fiscal_year": context.fiscal_year,
					"budget_subject": line.budget_subject,
					"funding_source": line.funding_source,
					"project": line.project,
					"period_start": line.period_start,
					"period_end": line.period_end,
				}
			)
			for line in source.snapshot.lines
		]
	)
