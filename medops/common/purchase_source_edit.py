"""Exact native PO comparison for existing immutable commitment bindings."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.organization import normalize_reference, normalize_required_date
from medops.common.reservation_commitment import HISTORICAL_ORDER_STATUSES

HEADER_FIELDS = ("name", "company", "supplier", "currency", "conversion_rate", "transaction_date")
ITEM_FIELDS = (
	"name",
	"parent",
	"parenttype",
	"parentfield",
	"docstatus",
	"material_request",
	"material_request_item",
	"item_code",
	"uom",
	"cost_center",
	"expense_account",
	"project",
	"qty",
	"rate",
	"amount",
	"base_amount",
	"is_free_item",
)
DECIMAL_FIELDS = frozenset({"conversion_rate", "qty", "rate", "amount", "base_amount"})


def _value(field, value):
	if field in DECIMAL_FIELDS:
		if type(value) not in (str, int, float, Decimal):
			raise ValueError("native exact decimal comparison required")
		return exact_decimal_amount(str(value), 9, maximum=MAX_APPROVAL_AMOUNT)
	if field == "transaction_date":
		return normalize_required_date(str(value), label=field)
	if field == "project" and value in (None, ""):
		return None
	if field == "docstatus":
		if not isinstance(value, (int, str)) or isinstance(value, bool) or value not in (1, "1"):
			raise ValueError("bound native child must remain submitted")
		return 1
	if field == "is_free_item":
		if value not in (None, "", 0, "0", False):
			raise ValueError("bound native purchase cannot become a free item")
		return 0
	name = normalize_reference(value, label=field)
	if name != value:
		raise ValueError("canonical native reference required")
	return name


def validate_bound_purchase_order_edit(
	before: Mapping[str, Any], after: Mapping[str, Any], bound_lines: tuple[str, ...]
) -> None:
	"""Allow native progress/unbound rows; never authorize new commitment or release."""
	if not bound_lines:
		return
	if type(bound_lines) is not tuple or len(bound_lines) != len(set(bound_lines)):
		raise ValueError("unique registered purchase lines required")
	for line in bound_lines:
		if normalize_reference(line, label="purchase_order_line") != line:
			raise ValueError("canonical registered purchase line required")
	for doc in (before, after):
		_value("docstatus", doc.get("docstatus"))
		if doc.get("status") not in HISTORICAL_ORDER_STATUSES:
			raise ValueError("bound purchase order must remain historically inspectable")
		if _value("conversion_rate", doc.get("conversion_rate")) != "1.000000000":
			raise ValueError("bound purchase order must retain request currency")
	for field in HEADER_FIELDS:
		if _value(field, before.get(field)) != _value(field, after.get(field)):
			raise ValueError("bound purchase header economic facts cannot change")
	for line in bound_lines:
		old = [row for row in before.get("items", ()) if row.get("name") == line]
		new = [row for row in after.get("items", ()) if row.get("name") == line]
		if len(old) != 1 or len(new) != 1:
			raise ValueError("bound purchase line cannot disappear or duplicate")
		for row in (old[0], new[0]):
			if (row.get("parent"), row.get("parenttype"), row.get("parentfield")) != (
				before.get("name"),
				"Purchase Order",
				"items",
			):
				raise ValueError("bound native child cannot move")
		for field in ITEM_FIELDS:
			if _value(field, old[0].get(field)) != _value(field, new[0].get(field)):
				raise ValueError("bound purchase line economic facts cannot change")
