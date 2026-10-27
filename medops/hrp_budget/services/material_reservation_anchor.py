"""Check reservation evidence against an already locked purchase source."""

from __future__ import annotations

from decimal import Decimal

from medops.common.budget_control import BudgetControlKey
from medops.common.budget_reservation_anchor import verify_reservation_anchor_row
from medops.common.budget_reservation_events import ReservationPosition
from medops.common.budget_reservation_proof import ReservationSource
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.organization import normalize_reference
from medops.hrp_budget.services.material_request_source import MaterialRequestSource


def verify_material_reservation_evidence(
	row: object,
	event_rows: object,
	*,
	source: MaterialRequestSource,
	allocation: object,
	expected_key: BudgetControlKey,
) -> ReservationPosition:
	"""Bind a selected aggregate and chain to purchase-line snapshot facts.

	The caller must obtain this snapshot from the locked native reader, derive
	the allocation from a trusted policy, independently establish the budget key,
	and independently prove the aggregate
	and complete event rows are authorized and locked. This function does not
	read the database, calculate a budget balance, or authorize a financial write.
	The full source digest serves as the source revision for this candidate.
	"""
	if type(source) is not MaterialRequestSource or type(expected_key) is not BudgetControlKey:
		raise ValueError("locked material request source and canonical budget key required")
	allocation_name = normalize_reference(allocation, label="allocation")
	if allocation_name != allocation or not source.expense_account:
		raise ValueError("canonical allocation and expense account required")
	amount = exact_decimal_amount(source.amount, 9, maximum=MAX_APPROVAL_AMOUNT)
	if amount != source.amount or Decimal(amount) <= 0:
		raise ValueError("positive exact purchase amount required")
	digest = source.digest()
	expected_source = ReservationSource(
		"Material Request", source.request, digest, source.line, allocation_name, "Reserve"
	)
	position = verify_reservation_anchor_row(
		row,
		event_rows,
		expected_source=expected_source,
		source_snapshot_digest=digest,
		cost_center=source.cost_center,
	)
	key = position.key
	if (
		key != expected_key
		or (key.company, key.hospital, key.organization_unit, key.project)
		!= (source.company, source.hospital, source.organization_unit, source.project)
		or position.currency != source.currency
		or Decimal(position.reserved) > Decimal(amount)
	):
		raise ValueError("reservation differs from purchase source")
	return position
