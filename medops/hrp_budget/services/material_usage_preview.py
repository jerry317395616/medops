"""Connect source locks, release evidence and usage inspection; no spending authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import frappe

from medops.common.budget_control import MAX_CONTROL_KEYS, budget_control_key, ordered_budget_keys
from medops.common.budget_material_match import (
	PurchaseBudgetCandidate,
	PurchaseMatchInputs,
	match_purchase_release_keys,
)
from medops.common.budget_reservation_events import ReservationPosition
from medops.common.organization import normalize_required_date
from medops.hrp_budget.services.control_guards import assert_budget_guards, invalidate_budget_guards
from medops.hrp_budget.services.control_inventory import guarded_release_inventory
from medops.hrp_budget.services.control_sources import resolve_budget_control_key
from medops.hrp_budget.services.material_authorization_observation import (
	verify_guarded_initial_authorizations,
)
from medops.hrp_budget.services.material_budget_guards import (
	MaterialSourceIdentity,
	MaterialSourceReference,
	acquire_material_budget_guards_for_sources,
)
from medops.hrp_budget.services.material_request_source import MaterialRequestSource
from medops.hrp_budget.services.material_usage_sources import (
	discover_material_usage_sources,
	guarded_verify_provisional_material_usage_sources,
)
from medops.services.errors import raise_ione_error


@dataclass(frozen=True, slots=True)
class PurchaseUsageInspection:
	source: MaterialRequestSource
	budget_date: str
	candidates: tuple[PurchaseBudgetCandidate, ...]
	usage: tuple[ReservationPosition, ...]


def _assert_actor(actor: str) -> None:
	if frappe.session.user != actor:
		raise_ione_error("CONFLICT")


def inspect_purchase_usage(
	request: str,
	line: str,
	organization_unit: str,
	*,
	expected_source_digest: str,
	budget_date: str,
	proposed_keys: object,
) -> PurchaseUsageInspection:
	"""Inspect released candidates and historical usage in one owned transaction.

	The caller supplies a previously obtained proposed-source digest and must
	begin before acquiring source or master locks. The caller must roll back on
	any failure. This is not an endpoint, a balance, an approval or permission to
	write; historical authorization and complete cooperating writers remain
	prerequisites. The pre-lock discovery tuple comes only from this function.
	"""
	try:
		actor = frappe.session.user
		proposed = MaterialSourceReference(request, line, organization_unit, expected_source_digest)
		date = normalize_required_date(budget_date, label="budget_date")
		if not isinstance(proposed_keys, (tuple, list)) or not 1 <= len(proposed_keys) <= MAX_CONTROL_KEYS:
			raise ValueError("bounded proposed budget keys required")
		inputs = tuple(asdict(budget_control_key(value)) for value in proposed_keys)
		keys = ordered_budget_keys([resolve_budget_control_key(value) for value in inputs])
		_assert_actor(actor)
		discovered = discover_material_usage_sources(keys)
		_assert_actor(actor)
		references: dict[tuple[str, str, str], MaterialSourceIdentity | MaterialSourceReference] = {
			(ref.request, ref.line, ref.organization_unit): MaterialSourceIdentity(
				ref.request, ref.line, ref.organization_unit
			)
			for ref in discovered
		}
		identity = (proposed.request, proposed.line, proposed.organization_unit)
		references[identity] = proposed
		sources = acquire_material_budget_guards_for_sources(tuple(references.values()), keys)
		_assert_actor(actor)
		selected = [
			source
			for source in sources
			if (source.request, source.line, source.organization_unit) == identity
		]
		if len(selected) != 1 or selected[0].digest() != proposed.expected_digest:
			raise ValueError("proposed source was not locked")
		source = selected[0]
		if not source.expense_account:
			raise ValueError("purchase expense account required")
		values: PurchaseMatchInputs = {
			"company": source.company,
			"hospital": source.hospital,
			"organization_unit": source.organization_unit,
			"project": source.project,
			"account": source.expense_account,
			"cost_center": source.cost_center,
			"currency": source.currency,
			"budget_date": date,
			"amount": source.amount,
		}
		match_purchase_release_keys(keys, [], **values)
		usage = guarded_verify_provisional_material_usage_sources(keys, sources, discovered=discovered)
		_assert_actor(actor)
		history = guarded_release_inventory(keys)
		_assert_actor(actor)
		candidates = match_purchase_release_keys(keys, history, **values)
		if guarded_verify_provisional_material_usage_sources(keys, sources, discovered=discovered) != usage:
			raise ValueError("usage changed during release inspection")
		verify_guarded_initial_authorizations(keys, sources, usage)
		if ordered_budget_keys([resolve_budget_control_key(value) for value in inputs]) != keys:
			raise ValueError("budget key references changed")
		_assert_actor(actor)
		assert_budget_guards(keys)
		return PurchaseUsageInspection(source, date, candidates, usage)
	except ValueError as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
