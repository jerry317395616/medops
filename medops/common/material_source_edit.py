"""Compare native source edits with registered immutable reservation bindings.

Float inputs here are native ERPNext document values, never spending amounts.
They are compared against exact persisted decimals and cannot authorize a write.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.material_source_snapshot import SUPPORTED_STATUSES
from medops.common.organization import normalize_reference, normalize_required_date

HEADER_FIELDS = ("name", "company", "material_request_type", "transaction_date")
ITEM_FIELDS = (
	"name",
	"item_code",
	"uom",
	"cost_center",
	"expense_account",
	"project",
	"qty",
	"rate",
	"amount",
)
MONEY_FIELDS = frozenset({"qty", "rate", "amount"})


def _value(field, value):
	if field in MONEY_FIELDS:
		if type(value) not in (str, int, float, Decimal):
			raise ValueError("native exact decimal comparison required")
		return exact_decimal_amount(str(value), 9, maximum=MAX_APPROVAL_AMOUNT)
	if field == "transaction_date":
		return normalize_required_date(str(value), label=field)
	if field in {"expense_account", "project"} and value in (None, ""):
		return None
	return normalize_reference(value, label=field)


def validate_bound_material_request_edit(
	before: Mapping[str, Any], after: Mapping[str, Any], bound_lines: tuple[str, ...]
) -> None:
	"""Permit operational progress and unbound-line changes, never rewrite a binding."""
	if not bound_lines:
		return
	if type(bound_lines) is not tuple or len(bound_lines) != len(set(bound_lines)):
		raise ValueError("unique registered source lines required")
	for line in bound_lines:
		if normalize_reference(line, label="source_line") != line:
			raise ValueError("canonical registered source line required")
	if (
		int(before.get("docstatus", -1)) != 1
		or int(after.get("docstatus", -1)) != 1
		or before.get("material_request_type") != "Purchase"
		or before.get("status") not in SUPPORTED_STATUSES
		or after.get("status") not in SUPPORTED_STATUSES
	):
		raise ValueError("registered purchase source must remain submitted and inspectable")
	for field in HEADER_FIELDS:
		if _value(field, before.get(field)) != _value(field, after.get(field)):
			raise ValueError("registered purchase header cannot change")
	for line in bound_lines:
		old = [row for row in before.get("items", ()) if row.get("name") == line]
		new = [row for row in after.get("items", ()) if row.get("name") == line]
		if len(old) != 1 or len(new) != 1:
			raise ValueError("registered purchase line cannot disappear or duplicate")
		for field in ITEM_FIELDS:
			if _value(field, old[0].get(field)) != _value(field, new[0].get(field)):
				raise ValueError("registered purchase line economic facts cannot change")
