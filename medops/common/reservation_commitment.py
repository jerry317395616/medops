"""Purchase commitment provenance and exact intent, never authority to post.

Callers must independently prove native rights, approval, complete stored
history, every earlier authorization, all PO allocations and current locks.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from decimal import Decimal

from medops.common.budget_control import BudgetControlKey, budget_control_key
from medops.common.budget_reservation_events import MAX_RESERVATION_EVENTS, ReservationPosition
from medops.common.budget_reservation_proof import (
	ReservationRecord,
	ReservationSource,
	verify_reservation_records,
)
from medops.common.domain_service import canonical_json, fingerprint_json, normalize_sha256
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.material_source_snapshot import (
	compare_material_source_history,
	parse_material_source_snapshot,
)
from medops.common.organization import normalize_reference, normalize_required_date, normalize_required_text
from medops.common.reservation_authorization import reservation_intent_snapshot

ORDER_FIELDS = frozenset(
	{
		"order",
		"line",
		"order_modified",
		"line_modified",
		"material_request",
		"material_request_line",
		"material_request_digest",
		"company",
		"hospital",
		"organization_unit",
		"cost_center",
		"expense_account",
		"project",
		"supplier",
		"currency",
		"transaction_date",
		"status",
		"item_code",
		"uom",
		"quantity",
		"rate",
		"amount",
		"base_amount",
	}
)
MONEY_FIELDS = frozenset({"quantity", "rate", "amount", "base_amount"})
ORDER_STATUSES = frozenset({"To Receive and Bill", "To Receive", "To Bill"})
# Native current history may advance beyond the states in which a new
# commitment can be authorized. Stored authorization snapshots remain strict.
HISTORICAL_ORDER_STATUSES = ORDER_STATUSES | frozenset(
	{"Completed", "Delivered", "To Pay", "On Hold", "Closed"}
)
MAX_ORDER_SNAPSHOT_BYTES = 32768


def _time(value):
	if not isinstance(value, str) or len(value) not in (19, 26):
		raise ValueError("canonical purchase order timestamp required")
	try:
		stamp = datetime.fromisoformat(value)
	except ValueError as error:
		raise ValueError("canonical purchase order timestamp required") from error
	if (
		stamp.tzinfo is not None
		or stamp.isoformat(sep=" ", timespec="seconds" if len(value) == 19 else "microseconds") != value
	):
		raise ValueError("canonical purchase order timestamp required")


def _order_facts(value, material_snapshot, material_digest):
	if normalize_sha256(material_digest, label="material_digest") != material_digest:
		raise ValueError("canonical material source digest required")
	material = parse_material_source_snapshot(material_snapshot, expected_digest=material_digest)
	if not isinstance(value, dict) or set(value) != ORDER_FIELDS:
		raise ValueError("complete purchase order facts required")
	facts = dict(value)
	excluded = MONEY_FIELDS | {
		"order_modified",
		"line_modified",
		"material_request_digest",
		"project",
		"transaction_date",
		"status",
	}
	for name in ORDER_FIELDS - excluded:
		if normalize_reference(facts[name], label=name) != facts[name]:
			raise ValueError("canonical purchase order reference required")
	if (
		facts["project"] is not None
		and normalize_reference(facts["project"], label="project") != facts["project"]
	):
		raise ValueError("canonical purchase project required")
	for name in ("order_modified", "line_modified"):
		_time(facts[name])
	if (
		normalize_required_date(facts["transaction_date"], label="transaction_date")
		!= facts["transaction_date"]
	):
		raise ValueError("canonical purchase order date required")
	if not isinstance(facts["status"], str) or facts["status"] not in ORDER_STATUSES:
		raise ValueError("supported open purchase order required")
	if facts["material_request_digest"] != material_digest:
		raise ValueError("purchase order differs from current material snapshot")
	for order_field, source_field in (
		("material_request", "request"),
		("material_request_line", "line"),
		*(
			(name, name)
			for name in (
				"company",
				"hospital",
				"organization_unit",
				"cost_center",
				"expense_account",
				"project",
				"currency",
				"item_code",
				"uom",
			)
		),
	):
		if facts[order_field] != material[source_field]:
			raise ValueError("purchase order differs from material request economics")
	for name in MONEY_FIELDS:
		if exact_decimal_amount(facts[name], 9, maximum=MAX_APPROVAL_AMOUNT) != facts[name]:
			raise ValueError("canonical exact purchase order amounts required")
	if (
		Decimal(facts["quantity"]) <= 0
		or Decimal(facts["quantity"]) > Decimal(material["quantity"])
		or Decimal(facts["amount"]) <= 0
		or facts["amount"] != facts["base_amount"]
	):
		raise ValueError("positive same-currency purchase line within request quantity required")
	return facts


def purchase_order_snapshot(value, *, material_snapshot, material_digest):
	"""Preserve the existing native PO digest format, bound to current MR facts.

	The native adapter must still prove submitted status, conversion rate one,
	permissions and fresh parent/child locks. A self-consistent snapshot does not.
	"""
	facts = _order_facts(value, material_snapshot, material_digest)
	payload = {"schema_version": 1, "kind": "purchase_order_source", **facts}
	text = canonical_json(payload)
	if len(text.encode("utf-8")) > MAX_ORDER_SNAPSHOT_BYTES:
		raise ValueError("bounded purchase order snapshot required")
	return text, fingerprint_json(payload)


def _unique_object(pairs):
	result = {}
	for name, value in pairs:
		if name in result:
			raise ValueError("duplicate purchase snapshot key")
		result[name] = value
	return result


def parse_purchase_order_snapshot(value, *, expected_digest, material_snapshot, material_digest):
	digest = normalize_sha256(expected_digest, label="purchase_order_digest")
	if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_ORDER_SNAPSHOT_BYTES:
		raise ValueError("bounded purchase order snapshot required")
	try:
		payload = json.loads(value, object_pairs_hook=_unique_object)
	except (ValueError, TypeError, RecursionError) as error:
		raise ValueError("invalid purchase order snapshot") from error
	if (
		not isinstance(payload, dict)
		or set(payload) != ORDER_FIELDS | {"schema_version", "kind"}
		or type(payload["schema_version"]) is not int
		or payload["schema_version"] != 1
		or payload["kind"] != "purchase_order_source"
		or canonical_json(payload) != value
		or not hmac.compare_digest(fingerprint_json(payload), digest)
	):
		raise ValueError("purchase order snapshot format or digest differs")
	return _order_facts({name: payload[name] for name in ORDER_FIELDS}, material_snapshot, material_digest)


@dataclass(frozen=True, slots=True)
class ReservationCommitmentIntent:
	reservation: str
	original_intent_digest: str
	original_source_digest: str
	current_source_digest: str
	order_snapshot_digest: str
	commitment_identity: str
	key: BudgetControlKey
	currency: str
	precision: int
	previous_sequence: int
	previous_event_digest: str
	previous_occurred_at: str
	source_modified_at: str
	open_before: str
	order_amount: str
	amount: str
	reason: str = field(repr=False)
	intent_digest: str


def reservation_commitment_intent(
	original, records, *, order_snapshot, order_digest, material_snapshot, material_digest, amount, reason
) -> ReservationCommitmentIntent:
	"""Derive a proposed conversion from complete supplied chain structure.

	No stored-history completeness, historical authority, cumulative PO allocation,
	human confirmation, current rights/locks or permission to write is implied.
	"""
	reservation_intent_snapshot(original)
	position = verify_reservation_records(records)
	material = parse_material_source_snapshot(material_snapshot, expected_digest=material_digest)
	first = records[0]
	if (
		first.source
		!= ReservationSource(
			"Material Request",
			material["request"],
			original.source_snapshot_digest,
			material["line"],
			original.allocation,
			"Reserve",
		)
		or first.event.kind != "Reserve"
		or first.event.amount != original.amount
	):
		raise ValueError("purchase commitment differs from original reservation source")
	return commitment_intent_for_position(
		original,
		position,
		records[-1],
		order_snapshot=order_snapshot,
		order_digest=order_digest,
		material_snapshot=material_snapshot,
		material_digest=material_digest,
		amount=amount,
		reason=reason,
	)


def commitment_intent_for_position(
	original,
	position,
	last,
	*,
	order_snapshot,
	order_digest,
	material_snapshot,
	material_digest,
	amount,
	reason,
):
	"""Internal linear-replay step; its supplied position is never authority."""
	reservation_intent_snapshot(original)
	if type(position) is not ReservationPosition or type(last) is not ReservationRecord:
		raise ValueError("checked commitment predecessor and position required")
	if normalize_sha256(order_digest, label="order_digest") != order_digest:
		raise ValueError("canonical purchase order digest required")
	material = parse_material_source_snapshot(material_snapshot, expected_digest=material_digest)
	compare_material_source_history(
		original.source_snapshot, expected_digest=original.source_snapshot_digest, current_facts=material
	)
	order = parse_purchase_order_snapshot(
		order_snapshot,
		expected_digest=order_digest,
		material_snapshot=material_snapshot,
		material_digest=material_digest,
	)
	if (
		position.reservation
		!= fingerprint_json(
			{"namespace": "budget-reservation-root-v1", "source_identity": original.reservation_identity}
		)
		or (position.key, position.currency, position.precision, position.reserved)
		!= (original.key, original.currency, original.precision, original.amount)
		or (
			last.event.reservation,
			last.event.sequence,
			last.event.key,
			last.event.currency,
			last.event.precision,
		)
		!= (position.reservation, position.last_sequence, position.key, position.currency, position.precision)
		or position.last_sequence >= MAX_RESERVATION_EVENTS
		or not original.key.period_start <= order["transaction_date"] <= original.key.period_end
	):
		raise ValueError("purchase commitment differs from original reservation or budget period")
	value = exact_decimal_amount(amount, original.precision, maximum=MAX_APPROVAL_AMOUNT)
	text = normalize_required_text(reason, label="reason", maximum=1000)
	if (
		value != amount
		or Decimal(value) <= 0
		or text != reason
		or Decimal(value) > Decimal(position.open_reservation)
		or Decimal(value) > Decimal(order["amount"])
	):
		raise ValueError("canonical positive commitment within reservation and purchase line required")
	item = ReservationCommitmentIntent(
		reservation=position.reservation,
		original_intent_digest=original.intent_digest,
		original_source_digest=original.source_snapshot_digest,
		current_source_digest=material_digest,
		order_snapshot_digest=order_digest,
		commitment_identity=fingerprint_json(
			{
				"namespace": "budget-purchase-order-commitment-v1",
				"order": order["order"],
				"line": order["line"],
			}
		),
		key=original.key,
		currency=original.currency,
		precision=original.precision,
		previous_sequence=position.last_sequence,
		previous_event_digest=last.event_digest,
		previous_occurred_at=last.occurred_at,
		source_modified_at=max(
			datetime.fromisoformat(value)
			for value in (
				material["request_modified"],
				material["line_modified"],
				order["order_modified"],
				order["line_modified"],
			)
		).isoformat(sep=" ", timespec="microseconds"),
		open_before=position.open_reservation,
		order_amount=order["amount"],
		amount=value,
		reason=text,
		intent_digest="",
	)
	return require_commitment_intent(replace(item, intent_digest=_commitment_digest(item)))


def _commitment_digest(item):
	payload = asdict(item)
	del payload["intent_digest"]
	return fingerprint_json(
		{"schema_version": 2, "kind": "reservation_commitment_intent", "action": "Commit", **payload}
	)


def require_commitment_intent(value):
	"""Validate self-consistency only; an independent caller must verify its sources."""
	if type(value) is not ReservationCommitmentIntent or type(value.key) is not BudgetControlKey:
		raise ValueError("canonical commitment intent required")
	if budget_control_key(asdict(value.key)) != value.key:
		raise ValueError("canonical commitment budget key required")
	for name in (
		"reservation",
		"original_intent_digest",
		"original_source_digest",
		"current_source_digest",
		"order_snapshot_digest",
		"commitment_identity",
		"previous_event_digest",
		"intent_digest",
	):
		if normalize_sha256(getattr(value, name), label=name) != getattr(value, name):
			raise ValueError("canonical commitment digest required")
	if (
		type(value.precision) is not int
		or not 0 <= value.precision <= 9
		or type(value.previous_sequence) is not int
		or not 1 <= value.previous_sequence < MAX_RESERVATION_EVENTS
		or normalize_reference(value.currency, label="currency") != value.currency
		or normalize_required_text(value.reason, label="reason", maximum=1000) != value.reason
	):
		raise ValueError("canonical commitment currency, sequence and reason required")
	for name in ("previous_occurred_at", "source_modified_at"):
		_time(getattr(value, name))
		if datetime.fromisoformat(getattr(value, name)).isoformat(
			sep=" ", timespec="microseconds"
		) != getattr(value, name):
			raise ValueError("canonical commitment microsecond time required")
	for name in ("open_before", "amount"):
		amount = getattr(value, name)
		if (
			exact_decimal_amount(amount, value.precision, maximum=MAX_APPROVAL_AMOUNT) != amount
			or Decimal(amount) <= 0
		):
			raise ValueError("canonical positive commitment amount required")
	if (
		exact_decimal_amount(value.order_amount, 9, maximum=MAX_APPROVAL_AMOUNT) != value.order_amount
		or Decimal(value.amount) > min(Decimal(value.open_before), Decimal(value.order_amount))
		or _commitment_digest(value) != value.intent_digest
	):
		raise ValueError("commitment differs from exact intent digest")
	return value


def reservation_commitment_snapshot(value):
	item = require_commitment_intent(value)
	return canonical_json(
		{"schema_version": 2, "kind": "reservation_commitment_intent", "action": "Commit", **asdict(item)}
	)


def parse_reservation_commitment_snapshot(value):
	if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_ORDER_SNAPSHOT_BYTES:
		raise ValueError("bounded commitment intent snapshot required")
	try:
		payload = json.loads(value, object_pairs_hook=_unique_object)
	except (TypeError, ValueError, RecursionError) as error:
		raise ValueError("invalid commitment intent snapshot") from error
	if (
		not isinstance(payload, dict)
		or set(payload)
		!= set(ReservationCommitmentIntent.__dataclass_fields__) | {"schema_version", "kind", "action"}
		or type(payload["schema_version"]) is not int
		or payload["schema_version"] != 2
		or payload["kind"] != "reservation_commitment_intent"
		or payload["action"] != "Commit"
		or canonical_json(payload) != value
	):
		raise ValueError("canonical commitment v2 snapshot required")
	key = budget_control_key(payload["key"])
	if asdict(key) != payload["key"]:
		raise ValueError("canonical commitment budget key required")
	for name in ("schema_version", "kind", "action"):
		del payload[name]
	payload["key"] = key
	return require_commitment_intent(ReservationCommitmentIntent(**payload))
