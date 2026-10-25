"""Bounded decimal wire values shared by planning and approval contracts."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

MAX_BUDGET_AMOUNT = Decimal("999999999999.999999999")
MAX_APPROVAL_AMOUNT = Decimal("999999999999999.999999999")


class ExactMoneyError(ValueError):
	"""An amount cannot be represented by the captured exact money contract."""


def canonical_decimal(amount: Decimal) -> str:
	if not amount.is_finite():
		raise ExactMoneyError("amount exceeds bounds")
	if not amount:
		return "0"
	text = format(amount, "f")
	return text.rstrip("0").rstrip(".") if "." in text else text


def exact_decimal_amount(
	value: object,
	precision: int | None = None,
	*,
	maximum: Decimal = MAX_BUDGET_AMOUNT,
) -> str:
	# Fractional JSON numbers may have lost digits before reaching this function.
	if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
		raise ExactMoneyError("decimal amount required")
	if len(str(value)) > 64:
		raise ExactMoneyError("amount exceeds bounds")
	try:
		amount = Decimal(str(value))
		if not amount.is_finite() or amount < 0 or amount > maximum:
			raise ExactMoneyError("amount exceeds bounds")
		if amount != amount.quantize(Decimal(".000000001")):
			raise ExactMoneyError("amount has unsupported precision")
		if precision is not None:
			if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 9:
				raise ExactMoneyError("unsupported currency precision")
			quantized = amount.quantize(Decimal(1).scaleb(-precision))
			if amount != quantized:
				raise ExactMoneyError("amount exceeds captured currency precision")
			return format(quantized, "f")
		return canonical_decimal(amount)
	except (InvalidOperation, ValueError) as exc:
		if isinstance(exc, ExactMoneyError):
			raise
		raise ExactMoneyError("invalid decimal amount") from exc
