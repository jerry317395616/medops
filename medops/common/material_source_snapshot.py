"""Canonical purchase-request snapshots; structure only, never authorization."""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any

from medops.common.budget_control import BudgetControlKey
from medops.common.budget_reservation_anchor import (
	ANCHOR_ROW_FIELDS,
	ReservationAnchor,
	reservation_anchor_row,
	verify_reservation_anchor,
	verify_reservation_anchor_row,
)
from medops.common.budget_reservation_events import ReservationPosition
from medops.common.budget_reservation_proof import ReservationSource
from medops.common.domain_service import canonical_json, fingerprint_json, normalize_sha256
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.organization import normalize_reference, normalize_required_date

SOURCE_FIELDS = frozenset(
	{
		"request",
		"line",
		"request_modified",
		"line_modified",
		"company",
		"hospital",
		"organization_unit",
		"organization_version",
		"mapping_revision",
		"cost_center",
		"currency",
		"transaction_date",
		"docstatus",
		"status",
		"item_code",
		"uom",
		"expense_account",
		"project",
		"quantity",
		"rate",
		"amount",
	}
)
REFERENCE_FIELDS = frozenset(
	{
		"request",
		"line",
		"company",
		"hospital",
		"organization_unit",
		"organization_version",
		"cost_center",
		"currency",
		"item_code",
		"uom",
		"expense_account",
	}
)
SUPPORTED_STATUSES = frozenset(
	{"Submitted", "Pending", "Partially Ordered", "Partially Received", "Ordered", "Received"}
)
MAX_SNAPSHOT_BYTES = 32768


def _timestamp(value: object) -> str:
	if not isinstance(value, str) or len(value) not in (19, 26):
		raise ValueError("canonical source timestamp required")
	try:
		stamp = datetime.fromisoformat(value)
	except ValueError as error:
		raise ValueError("invalid source timestamp") from error
	if (
		stamp.tzinfo is not None
		or stamp.isoformat(sep=" ", timespec="seconds" if len(value) == 19 else "microseconds") != value
	):
		raise ValueError("canonical source timestamp required")
	return value


def _validate_facts(value: object) -> dict[str, Any]:
	if not isinstance(value, dict) or set(value) != SOURCE_FIELDS:
		raise ValueError("complete material source facts required")
	facts = dict(value)
	for field in REFERENCE_FIELDS:
		if normalize_reference(facts[field], label=field) != facts[field]:
			raise ValueError("canonical material source reference required")
	if (
		facts["project"] is not None
		and normalize_reference(facts["project"], label="project") != facts["project"]
	):
		raise ValueError("canonical project required")
	for field in ("request_modified", "line_modified"):
		_timestamp(facts[field])
	if (
		normalize_required_date(facts["transaction_date"], label="transaction_date")
		!= facts["transaction_date"]
	):
		raise ValueError("canonical source date required")
	if type(facts["mapping_revision"]) is not int or facts["mapping_revision"] < 1:
		raise ValueError("positive mapping revision required")
	if (
		type(facts["docstatus"]) is not int
		or facts["docstatus"] != 1
		or not isinstance(facts["status"], str)
		or facts["status"] not in SUPPORTED_STATUSES
	):
		raise ValueError("supported submitted purchase request required")
	for field in ("quantity", "rate", "amount"):
		if exact_decimal_amount(facts[field], 9, maximum=MAX_APPROVAL_AMOUNT) != facts[field]:
			raise ValueError("canonical exact source amount required")
	if Decimal(facts["quantity"]) <= 0 or Decimal(facts["amount"]) <= 0:
		raise ValueError("positive source quantity and amount required")
	return facts


def material_source_snapshot(value: object) -> tuple[str, str]:
	"""Encode supplied facts in the existing source-digest format.

	The caller must obtain facts from a permission-checked locked source. This
	function does not prove origin, actor, approval, policy or database integrity.
	"""
	facts = _validate_facts(value)
	payload = {"schema_version": 1, "kind": "material_request_source", **facts}
	return canonical_json(payload), fingerprint_json(payload)


def _unique_object(pairs):
	result = {}
	for key, value in pairs:
		if key in result:
			raise ValueError("duplicate snapshot key")
		result[key] = value
	return result


def parse_material_source_snapshot(value: object, *, expected_digest: object) -> dict[str, Any]:
	"""Parse canonical facts bound to an independently obtained digest.

	Matching a digest stored alongside the payload proves only self-consistency.
	The caller must independently authenticate the authorization and its digest.
	"""
	digest = normalize_sha256(expected_digest, label="expected_digest")
	if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
		raise ValueError("bounded snapshot text required")
	try:
		payload = json.loads(value, object_pairs_hook=_unique_object)
	except (TypeError, ValueError, RecursionError) as error:
		raise ValueError("invalid material source snapshot") from error
	if (
		not isinstance(payload, dict)
		or set(payload) != SOURCE_FIELDS | {"schema_version", "kind"}
		or type(payload["schema_version"]) is not int
		or payload["schema_version"] != 1
		or payload["kind"] != "material_request_source"
		or canonical_json(payload) != value
		or not hmac.compare_digest(fingerprint_json(payload), digest)
	):
		raise ValueError("snapshot format or digest differs")
	return _validate_facts({field: payload[field] for field in SOURCE_FIELDS})


@dataclass(frozen=True, slots=True)
class MaterialSourceComparison:
	historical_digest: str
	current_digest: str
	status_changed: bool
	timestamps_changed: bool


def compare_material_source_history(
	historical_snapshot: object, *, expected_digest: object, current_facts: object
) -> MaterialSourceComparison:
	"""Distinguish status/timestamp changes from changes to all other facts.

	This comparison never establishes that the historical snapshot, status change,
	approval or caller is trustworthy. A persisted, independently authenticated
	decision and complete source lifecycle are mandatory before financial use.
	"""
	historical = parse_material_source_snapshot(historical_snapshot, expected_digest=expected_digest)
	current = _validate_facts(current_facts)
	progress = {"request_modified", "line_modified", "status"}
	if any(historical[field] != current[field] for field in SOURCE_FIELDS - progress):
		raise ValueError("material source financial facts changed")
	_, current_digest = material_source_snapshot(current)
	return MaterialSourceComparison(
		historical_digest=normalize_sha256(expected_digest, label="expected_digest"),
		current_digest=current_digest,
		status_changed=historical["status"] != current["status"],
		timestamps_changed=(historical["request_modified"], historical["line_modified"])
		!= (current["request_modified"], current["line_modified"]),
	)


def _check_position(
	position: ReservationPosition, facts: dict[str, Any], expected_key: BudgetControlKey
) -> None:
	if type(expected_key) is not BudgetControlKey or position.key != expected_key:
		raise ValueError("reservation budget key differs")
	key = position.key
	if (key.company, key.hospital, key.organization_unit, key.project) != (
		facts["company"],
		facts["hospital"],
		facts["organization_unit"],
		facts["project"],
	) or position.currency != facts["currency"]:
		raise ValueError("reservation scope differs from historical source")
	with localcontext() as context:
		context.prec = 48
		exposure = (
			Decimal(position.open_reservation) + Decimal(position.open_commitment) + Decimal(position.actual)
		)
		if exposure > Decimal(facts["amount"]):
			raise ValueError("reservation exceeds historical source amount")


def build_snapshot_reservation_projection(
	anchor: object,
	records: object,
	*,
	source_facts: object,
	allocation: object,
	expected_key: BudgetControlKey,
) -> dict[str, object]:
	"""Build a snapshot-bearing projection, without persistence or authority.

	The source, allocation and expected key must be independently obtained under
	appropriate locks and policy. The event chain's authorization, complete
	history and the human decision are not proved by this projection or its hash.
	"""
	if type(anchor) is not ReservationAnchor:
		raise ValueError("reservation anchor required")
	facts = _validate_facts(source_facts)
	allocation_name = normalize_reference(allocation, label="allocation")
	if allocation_name != allocation:
		raise ValueError("canonical allocation required")
	payload, digest = material_source_snapshot(facts)
	expected_source = ReservationSource(
		"Material Request", facts["request"], digest, facts["line"], allocation_name, "Reserve"
	)
	position = verify_reservation_anchor(
		anchor,
		records,
		expected_source=expected_source,
		source_snapshot_digest=digest,
		cost_center=facts["cost_center"],
	)
	_check_position(position, facts, expected_key)
	return {**reservation_anchor_row(anchor), "source_snapshot_json": payload}


def verify_snapshot_reservation_projection(
	row: object,
	event_rows: object,
	*,
	expected_snapshot_digest: object,
	allocation: object,
	expected_key: BudgetControlKey,
	current_facts: object,
) -> tuple[ReservationPosition, MaterialSourceComparison]:
	"""Compare an exact projection to independently authorized historical facts.

	The expected digest must come from an independently verified immutable human
	decision, never from this row. The caller must authenticate and lock sources,
	prove complete chains and permitted lifecycle events, and hold all guards.
	This pure comparison does none of those things and grants no spending rights.
	"""
	if not isinstance(row, dict) or set(row) != ANCHOR_ROW_FIELDS | {"source_snapshot_json"}:
		raise ValueError("complete snapshot reservation projection required")
	digest = normalize_sha256(expected_snapshot_digest, label="expected_snapshot_digest")
	historical = parse_material_source_snapshot(row["source_snapshot_json"], expected_digest=digest)
	allocation_name = normalize_reference(allocation, label="allocation")
	if allocation_name != allocation:
		raise ValueError("canonical allocation required")
	comparison = compare_material_source_history(
		row["source_snapshot_json"], expected_digest=digest, current_facts=current_facts
	)
	expected_source = ReservationSource(
		"Material Request", historical["request"], digest, historical["line"], allocation_name, "Reserve"
	)
	projection = {key: row[key] for key in ANCHOR_ROW_FIELDS}
	position = verify_reservation_anchor_row(
		projection,
		event_rows,
		expected_source=expected_source,
		source_snapshot_digest=digest,
		cost_center=historical["cost_center"],
	)
	_check_position(position, historical, expected_key)
	return position, comparison
