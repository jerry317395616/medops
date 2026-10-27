"""Connect a locked purchase source to guarded, verified release evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from medops.common.budget_control import MAX_CONTROL_KEYS, budget_control_key, ordered_budget_keys
from medops.common.budget_material_match import (
	PurchaseBudgetCandidate,
	PurchaseMatchInputs,
	match_purchase_release_keys,
)
from medops.common.domain_service import normalize_sha256
from medops.common.organization import normalize_required_date
from medops.hrp_budget.services.control_guards import (
	acquire_budget_guards,
	assert_budget_guards,
	invalidate_budget_guards,
)
from medops.hrp_budget.services.control_inventory import guarded_release_inventory
from medops.hrp_budget.services.control_sources import resolve_budget_control_key
from medops.hrp_budget.services.material_request_source import (
	MaterialRequestSource,
	locked_material_request_source,
)
from medops.services.errors import raise_ione_error


@dataclass(frozen=True, slots=True)
class PurchaseReleaseMatch:
	source: MaterialRequestSource
	budget_date: str
	candidates: tuple[PurchaseBudgetCandidate, ...]


def prepare_purchase_release_match(
	request: object,
	line: object,
	organization_unit: object,
	*,
	expected_source_digest: object,
	budget_date: object,
	proposed_keys: object,
) -> PurchaseReleaseMatch:
	"""Recheck a source and proposed released keys inside the budget guard.

	This is not a public endpoint and does not authorize a reservation or expose
	a balance. The caller must roll back the entire transaction on any failure.
	It cannot establish hospital allocation policy, historic currency, complete
	budget usage, or global lock ordering while other writers bypass the guard.
	"""
	try:
		digest = normalize_sha256(expected_source_digest, label="expected_source_digest")
		date = normalize_required_date(budget_date, label="budget_date")
		if not isinstance(proposed_keys, (tuple, list)) or not 1 <= len(proposed_keys) <= MAX_CONTROL_KEYS:
			raise ValueError("bounded proposed budget keys required")
		inputs = tuple(asdict(budget_control_key(value)) for value in proposed_keys)
		source = locked_material_request_source(request, line, organization_unit)
		account = source.expense_account
		if source.digest() != digest or not account:
			raise_ione_error("CONFLICT")
		keys = ordered_budget_keys([resolve_budget_control_key(value) for value in inputs])
		values: PurchaseMatchInputs = {
			"company": source.company,
			"hospital": source.hospital,
			"organization_unit": source.organization_unit,
			"project": source.project,
			"account": account,
			"cost_center": source.cost_center,
			"currency": source.currency,
			"budget_date": date,
			"amount": source.amount,
		}
		# Reject unscoped or out-of-period proposals before acquiring any guard.
		match_purchase_release_keys(keys, [], **values)
		acquire_budget_guards(keys)
		current = locked_material_request_source(request, line, organization_unit)
		if current != source or current.digest() != digest:
			raise_ione_error("CONFLICT")
		if ordered_budget_keys([resolve_budget_control_key(value) for value in inputs]) != keys:
			raise_ione_error("CONFLICT")
		history = guarded_release_inventory(keys)
		candidates = match_purchase_release_keys(keys, history, **values)
		# Inventory can take time; refresh the actor and native source permissions
		# again before returning facts that were read earlier in this transaction.
		current = locked_material_request_source(request, line, organization_unit)
		if current != source or current.digest() != digest:
			raise_ione_error("CONFLICT")
		if ordered_budget_keys([resolve_budget_control_key(value) for value in inputs]) != keys:
			raise_ione_error("CONFLICT")
		assert_budget_guards(keys)
		return PurchaseReleaseMatch(current, date, candidates)
	except ValueError as error:
		invalidate_budget_guards()
		raise_ione_error("INVALID_REQUEST", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
