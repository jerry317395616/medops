"""Preliminary guarded resolution of release keys; no authority to post."""

from __future__ import annotations

import frappe

from medops.common.budget_control_history import require_compatible_release_history
from medops.common.budget_ledger import BudgetReleaseFacts
from medops.common.organization import normalize_positive_integer, normalize_reference
from medops.hrp_budget.services.control_guards import (
	acquire_budget_guards,
	assert_budget_guards,
	invalidate_budget_guards,
)
from medops.hrp_budget.services.control_sources import resolve_release_control_keys
from medops.hrp_budget.services.ledger_integrity import complete_batch
from medops.hrp_budget.services.release import lock_release_source
from medops.hrp_budget.services.release_access import get_release
from medops.hrp_budget.services.release_confirmation import require_financial_actor
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


def prepare_persisted_release_guards(release: object, expected_revision: object):
	"""Lock and recheck one authorized release and its original batch if reversing.

	This is an internal candidate, not an authorization to post. The caller must
	still validate the execution lease and confirmation, enumerate every related
	budget event, enforce overlap and balance rules, and use one shared lock order
	for every writer. Full rollback is required after any failure.
	"""
	# Imported lazily to avoid a cycle when the existing execution service later
	# incorporates this candidate. It refreshes current actor and source grants.
	from medops.hrp_budget.services.release_execution import refresh_financial_authority

	name = normalize_reference(release, label="release")
	revision = normalize_positive_integer(expected_revision, label="expected_revision")
	actor = frappe.session.user
	refresh_financial_authority(actor)
	doc = lock_release_source(name)
	if doc.status != "Authorized" or int(doc.docstatus) != 0 or int(doc.revision) != revision:
		raise_ione_error("CONFLICT")
	facts = require_financial_actor(doc)
	original = None
	if facts.operation == "Reverse":
		original_doc = get_release(facts.reversal_of, lock=True)
		original, _, _ = complete_batch(original_doc)
	keys = prepare_release_guards(facts, original=original)
	try:
		if frappe.session.user != actor:
			raise_ione_error("CONFLICT")
		refresh_financial_authority(actor)
		current = get_release(doc.name, lock=True)
		if (
			current.status != "Authorized"
			or int(current.docstatus) != 0
			or int(current.revision) != revision
			or require_financial_actor(current) != facts
		):
			raise_ione_error("CONFLICT")
		if original is not None:
			current_original, _, _ = complete_batch(get_release(facts.reversal_of, lock=True))
			if current_original != original:
				raise_ione_error("CONFLICT")
		assert_budget_guards(keys)
	except BaseException:
		invalidate_budget_guards()
		raise
	return keys


def prepare_persisted_release_inventory(release: object, expected_revision: object):
	"""Reconcile allocation history and reject ambiguous period/currency intent.

	This candidate does not authorize a financial write or a reversal: other
	budget event types, shared writer fencing, balance, the execution lease and
	transaction-wide retry still require implementation and validation.
	"""
	from medops.hrp_budget.services.control_inventory import guarded_release_inventory
	from medops.hrp_budget.services.planning import native_document

	keys = prepare_persisted_release_guards(release, expected_revision)
	try:
		entries = guarded_release_inventory(keys)
		doc = get_release(normalize_reference(release, label="release"), lock=True)
		revision = normalize_positive_integer(expected_revision, label="expected_revision")
		if doc.status != "Authorized" or int(doc.docstatus) != 0 or int(doc.revision) != revision:
			raise_ione_error("CONFLICT")
		facts = require_financial_actor(doc)
		currency = native_document("Currency", facts.snapshot.context.currency, {"name"}).name
		try:
			require_compatible_release_history(
				keys, entries, currency=currency, precision=facts.snapshot.context.currency_precision
			)
		except ValueError as error:
			raise_ione_error("CONFLICT", cause=error)
		assert_budget_guards(keys)
		return keys, entries
	except BaseException:
		invalidate_budget_guards()
		raise
