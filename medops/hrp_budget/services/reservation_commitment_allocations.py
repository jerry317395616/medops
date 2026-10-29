"""Native whole-order allocation discovery and exact cross-root capacity checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, localcontext

import frappe

from medops.common.budget_reservation_events import MAX_RESERVATION_EVENTS
from medops.common.budget_reservation_storage import ROW_FIELDS, reservation_record_from_row
from medops.common.commitment_evidence import (
	COMMITMENT_ROW_FIELDS,
	commitment_sources,
	verify_commitment_evidence_row,
)
from medops.common.domain_service import fingerprint_json
from medops.common.exact_money import exact_decimal_amount
from medops.common.organization import normalize_reference
from medops.common.reservation_commitment import require_commitment_intent, reservation_commitment_intent
from medops.hrp_budget.services import material_authorization_observation as history
from medops.hrp_budget.services import material_usage_sources
from medops.hrp_budget.services.commitment_authorization import require_commitment_evidence_storage
from medops.hrp_budget.services.control_guards import (
	assert_budget_guards,
	invalidate_budget_guards,
	require_purchase_source_lock_order,
)
from medops.hrp_budget.services.material_usage_preview import PurchaseUsageInspection
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.purchase_order_source import PurchaseOrderSource, locked_purchase_order_source
from medops.hrp_budget.services.reservation_release_state import read_reservation_original
from medops.services.errors import raise_ione_error, require_roles

TABLE = "tabHRP Budget Commitment Authorization"
MAX_ORDER_DECISIONS = 10000
QUERY_CHUNK = 100


def _columns(fields):
	return ", ".join(f"`{name}`" for name in sorted(fields))


def read_purchase_commitment_pairs(order, line, *, lock=False):
	"""Read canonical pairs through both order and stable-line indexes.

	Caller must authorize the current native PO and budget scope before this
	private read. Discovery is not authority: every relevant root still needs
	complete guarded history. All rows for the order are checked before filtering
	other lines, so a malformed row cannot hide behind an inconsistent line ID.
	"""
	for value, label in ((order, "purchase_order"), (line, "purchase_order_line")):
		if normalize_reference(value, label=label) != value:
			raise ValueError("canonical purchase identity required")
	if type(lock) is not bool:
		raise ValueError("explicit purchase allocation lock option required")
	require_commitment_evidence_storage()
	identity = fingerprint_json(
		{"namespace": "budget-purchase-order-commitment-v1", "order": order, "line": line}
	)
	suffix = " FOR UPDATE" if lock else ""
	found = {}
	for column, value in (("purchase_order", order), ("commitment_identity", identity)):
		rows = frappe.db.sql(
			f"SELECT {_columns(COMMITMENT_ROW_FIELDS)} FROM `{TABLE}` WHERE `{column}`=%s "  # nosec B608 - fixed table/column/field sets
			f"AND `event_kind`=%s ORDER BY `name` LIMIT %s{suffix}",
			(value, "Decision", MAX_ORDER_DECISIONS + 1),
			as_dict=True,
		)
		page = set()
		for row in rows:
			name = row.get("name")
			if (
				not isinstance(name, str)
				or not name
				or name in page
				or (name in found and found[name] != row)
			):
				raise ValueError("unique unchanged purchase decision rows required")
			page.add(name)
			found[name] = row
			if len(found) > MAX_ORDER_DECISIONS:
				raise ValueError("purchase allocation decision count exceeds bound")
	names = [found[name].get("confirmation") for name in sorted(found)]
	if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
		raise ValueError("unique purchase confirmation references required")
	confirmations = {}
	for start in range(0, len(names), QUERY_CHUNK):
		chunk = names[start : start + QUERY_CHUNK]
		placeholders = ", ".join(["%s"] * len(chunk))
		rows = frappe.db.sql(
			f"SELECT {_columns(COMMITMENT_ROW_FIELDS)} FROM `{TABLE}` WHERE `name` IN ({placeholders}) "  # nosec B608 - fixed fields/bound names
			f"ORDER BY `name`{suffix}",
			tuple(chunk),
			as_dict=True,
		)
		for row in rows:
			if row["name"] in confirmations:
				raise ValueError("purchase confirmation cannot be reused")
			confirmations[row["name"]] = row
	if set(confirmations) != set(names):
		raise ValueError("complete independent purchase confirmations required")
	result = []
	for name in sorted(found):
		second = found[name]
		first = confirmations[second["confirmation"]]
		item, confirmed = verify_commitment_evidence_row(first)
		checked, decided = verify_commitment_evidence_row(second, confirmed)
		if checked != item:
			raise ValueError("purchase confirmation and decision intents differ")
		if item.commitment_identity == identity:
			if (first["purchase_order"], first["purchase_order_line"]) != (order, line):
				raise ValueError("stable purchase identity differs from physical line")
			result.append((item, confirmed, decided, first, second))
	return tuple(result)


@dataclass(frozen=True, slots=True)
class PurchaseCommitmentCapacity:
	commitment_identity: str
	order_snapshot_digest: str
	committed: str
	remaining: str
	decision_count: int
	decision_set_digest: str = field(repr=False)


def _read_selected_records(item):
	rows = frappe.db.sql(
		f"SELECT {_columns(ROW_FIELDS)} FROM `tabHRP Budget Reservation Event` "  # nosec B608 - fixed fields/bound root
		"WHERE `reservation`=%s ORDER BY `sequence`,`name` LIMIT %s FOR UPDATE",
		(item.reservation, MAX_RESERVATION_EVENTS + 1),
		as_dict=True,
	)
	if len(rows) != item.previous_sequence:
		raise ValueError("complete selected reservation predecessor required")
	return tuple(reservation_record_from_row(row) for row in rows)


def verify_guarded_commitment_capacity(inspection, purchase, proposed):
	"""Re-read all relevant roots and PO decisions, then check the global line cap.

	The inspector must have prelocked the PO parent and the complete source/key
	set in an owned transaction. Missing outside-scope allocations fail instead
	of being ignored. This observes capacity, never grants human or write authority.
	Any failure invalidates the active guard and requires a full rollback.
	"""
	try:
		require_roles(MANAGER_ROLES)
		actor = frappe.session.user
		if type(inspection) is not PurchaseUsageInspection or type(purchase) is not PurchaseOrderSource:
			raise ValueError("native complete purchase inspection required")
		item = require_commitment_intent(proposed)
		assert_budget_guards(inspection.keys)
		material = inspection.source
		require_purchase_source_lock_order(
			purchase.order, (material.request, material.line, material.organization_unit, material.digest())
		)
		if (material, purchase) != locked_purchase_order_source(
			purchase.order,
			purchase.line,
			material.request,
			material.line,
			material.organization_unit,
			expected_request_digest=material.digest(),
		):
			raise ValueError("current purchase source changed")
		require_roles(MANAGER_ROLES)
		pairs = read_purchase_commitment_pairs(purchase.order, purchase.line, lock=True)
		current = material_usage_sources.guarded_verify_provisional_material_usage_sources(
			inspection.keys,
			inspection.sources,
			discovered=inspection.discovered,
		)
		if current != inspection.usage:
			raise ValueError("complete budget usage changed")
		history.verify_guarded_initial_authorizations(inspection.keys, inspection.sources, current)
		by_root = {position.reservation: position for position in current}
		if item.reservation not in by_root or item.key != by_root[item.reservation].key:
			raise ValueError("selected reservation is outside complete guarded usage")
		_, original, _, _ = read_reservation_original(item.reservation, lock=True)
		records = _read_selected_records(item)
		order_snapshot, order_digest = purchase.commitment_snapshot(material)
		from medops.common.material_source_snapshot import material_source_snapshot

		material_snapshot, material_digest = material_source_snapshot(asdict(material))
		expected = reservation_commitment_intent(
			original,
			records,
			order_snapshot=order_snapshot,
			order_digest=order_digest,
			material_snapshot=material_snapshot,
			material_digest=material_digest,
			amount=item.amount,
			reason=item.reason,
		)
		if item != expected:
			raise ValueError("proposed commitment differs from current independent history")
		with localcontext() as context:
			context.prec = 48
			total = Decimal(0)
			for committed_item, _, _, first, _ in pairs:
				position = by_root.get(committed_item.reservation)
				if position is None or (position.key, position.currency, position.precision) != (
					committed_item.key,
					committed_item.currency,
					committed_item.precision,
				):
					raise ValueError("purchase allocation root is outside complete guarded history")
				old = commitment_sources(
					committed_item, first["material_snapshot_json"], first["order_snapshot_json"]
				)
				allowed_progress = {"order_modified", "line_modified", "status", "material_request_digest"}
				if {k: v for k, v in old.items() if k not in allowed_progress} != {
					k: v for k, v in asdict(purchase).items() if k not in allowed_progress
				}:
					raise ValueError("committed purchase economic facts changed")
				total += Decimal(committed_item.amount)
			limit = Decimal(purchase.amount)
			if total > limit or total + Decimal(item.amount) > limit:
				raise ValueError("purchase line commitment capacity exceeded")
			committed = exact_decimal_amount(total, 9)
			remaining = exact_decimal_amount(limit - total, 9)
		if pairs != read_purchase_commitment_pairs(purchase.order, purchase.line, lock=True):
			raise ValueError("purchase allocation membership changed")
		if (material, purchase) != locked_purchase_order_source(
			purchase.order,
			purchase.line,
			material.request,
			material.line,
			material.organization_unit,
			expected_request_digest=material.digest(),
		) or frappe.session.user != actor:
			raise ValueError("purchase source or actor changed")
		require_roles(MANAGER_ROLES)
		assert_budget_guards(inspection.keys)
		return PurchaseCommitmentCapacity(
			purchase.commitment_identity(),
			purchase.digest(),
			committed,
			remaining,
			len(pairs),
			fingerprint_json(
				[
					{"intent": i.intent_digest, "confirmation": c.event_digest, "decision": d.event_digest}
					for i, c, d, _, _ in pairs
				]
			),
		)
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
