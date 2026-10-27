"""Bounded complete-book contracts and exact totals; no database or grants."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from hashlib import sha256
from types import MappingProxyType

from medops.common.budget_ledger import LEDGER_PUBLIC_FIELDS
from medops.common.budget_planning import object_payload
from medops.common.domain_service import DomainServiceContractError, canonical_json, normalize_sha256
from medops.common.exact_money import signed_decimal_amount
from medops.common.organization import (
	normalize_nonnegative_integer,
	normalize_optional_date,
	normalize_reference,
	normalize_required_date,
	normalize_required_text,
)

MAX_STATEMENT_ENTRIES = 50000
MAX_STATEMENT_PAGE = 50
STATEMENT_SCHEMA_VERSION = 1
DISPLAY_LABEL_FIELDS = ("budget_subject", "funding_source")


def with_display_labels(
	result: dict[str, object], labels: Sequence[Mapping[str, object]]
) -> dict[str, object]:
	"""Attach optional page-only names without reducing the existing JSON budget."""
	rows = result.get("rows")
	if not isinstance(rows, list) or len(labels) != len(rows):
		raise ValueError("display labels must match the current page")
	clean = []
	for row in labels:
		if set(row) != set(DISPLAY_LABEL_FIELDS):
			raise ValueError("invalid display label fields")
		clean.append(
			{
				key: None if row[key] is None else normalize_required_text(row[key], label=key, maximum=140)
				for key in DISPLAY_LABEL_FIELDS
			}
		)
	if not any(value is not None for row in clean for value in row.values()):
		return result
	candidate = dict(result, display_labels=clean)
	try:
		canonical_json(candidate)
	except DomainServiceContractError:
		# A previously valid full page must not fail solely due to optional labels.
		return result
	return candidate


@dataclass(frozen=True, slots=True)
class StatementQuery:
	organization_unit: str
	fiscal_year: str
	currency: str
	from_date: str | None = None
	to_date: str | None = None


@dataclass(frozen=True, slots=True)
class StatementRequest:
	query: StatementQuery
	offset: int
	limit: int
	expected_digest: str | None
	export: bool = False


def build_statement_request(
	payload: object,
	*,
	offset: object = 0,
	limit: object = 50,
	expected_digest: object = None,
	export: bool = False,
) -> StatementRequest:
	data = object_payload(payload, {"organization_unit", "fiscal_year", "currency", "from_date", "to_date"})
	query = StatementQuery(
		organization_unit=normalize_reference(data.get("organization_unit"), label="organization_unit"),
		fiscal_year=normalize_reference(data.get("fiscal_year"), label="fiscal_year"),
		currency=normalize_reference(data.get("currency"), label="currency"),
		from_date=normalize_optional_date(data.get("from_date"), label="from_date"),
		to_date=normalize_optional_date(data.get("to_date"), label="to_date"),
	)
	if query.from_date and query.to_date and query.from_date > query.to_date:
		raise ValueError("invalid statement date range")
	start = normalize_nonnegative_integer(offset, label="offset")
	length = normalize_nonnegative_integer(limit, label="limit")
	if start > MAX_STATEMENT_ENTRIES or not 1 <= length <= MAX_STATEMENT_PAGE or type(export) is not bool:
		raise ValueError("statement bounds are invalid")
	digest = (
		normalize_sha256(expected_digest, label="expected_digest")
		if expected_digest not in (None, "")
		else None
	)
	if (start or export) and not digest:
		raise ValueError("subsequent pages and export require the complete statement digest")
	return StatementRequest(query, start, length, digest, export)


def statement_dates(query: StatementQuery, year_start, year_end):
	start = normalize_required_date(year_start, label="year_start_date")
	end = normalize_required_date(year_end, label="year_end_date")
	first = query.from_date or start
	last = query.to_date or end
	if not start <= first <= last <= end:
		raise ValueError("statement dates are outside the fiscal year")
	return first, last


@dataclass(frozen=True, slots=True)
class CompleteStatement:
	context: Mapping[str, object]
	entries: tuple[Mapping[str, object], ...]
	rows: tuple[Mapping[str, object], ...]
	totals: Mapping[str, object]
	digest: str


def complete_statement(context: Mapping[str, object], entries: Sequence[Mapping[str, object]]):
	if len(entries) > MAX_STATEMENT_ENTRIES:
		raise ValueError("complete statement exceeds the declared bound")
	first = normalize_required_date(context["from_date"], label="from_date")
	last = normalize_required_date(context["to_date"], label="to_date")
	if first > last:
		raise ValueError("invalid statement range")
	ordered = sorted(
		entries, key=lambda row: (str(row["posting_date"]), str(row["posting_time"]), str(row["name"]))
	)
	if len({row["name"] for row in ordered}) != len(ordered):
		raise ValueError("duplicate statement entry")
	precision = 0
	with localcontext() as decimal_context:
		decimal_context.prec = 48
		opening = debit = credit = Decimal(0)
		for row in ordered:
			if set(row) != set(LEDGER_PUBLIC_FIELDS):
				raise ValueError("only the reviewed original-leaf projection may enter a statement")
			level = row["currency_precision"]
			if type(level) is not int or not 0 <= level <= 9:
				raise ValueError("invalid captured statement precision")
			precision = max(precision, level)
			if row["amount_schema_version"] != 2 or any(
				row[key] != context[key]
				for key in (
					"company",
					"hospital",
					"organization_version",
					"organization_unit",
					"fiscal_year",
					"currency",
				)
			):
				raise ValueError("statement entry context differs")
			amount = Decimal(signed_decimal_amount(row["amount"], precision=level))
			left = Decimal(signed_decimal_amount(row["debit"], precision=level))
			right = Decimal(signed_decimal_amount(row["credit"], precision=level))
			if (
				left < 0
				or right < 0
				or amount != left - right
				or not str(context["fiscal_year_start"]) <= str(row["posting_date"]) <= last
			):
				raise ValueError("invalid statement accounting facts")
			if str(row["posting_date"]) < first:
				opening += amount
			else:
				debit += left
				credit += right

		def money(value: Decimal) -> str:
			if value == 0:
				value = abs(value)
			return format(value.quantize(Decimal(1).scaleb(-precision)), f".{precision}f")

		totals = dict(
			precision=precision,
			opening=money(opening),
			debit=money(debit),
			credit=money(credit),
			net=money(debit - credit),
			closing=money(opening + debit - credit),
		)
		balance = opening
		rows = []
		for row in ordered:
			if str(row["posting_date"]) >= first:
				balance += Decimal(str(row["amount"]))
				rows.append(MappingProxyType(dict(row, running_balance=money(balance))))
		digest = sha256()
		for value in (
			dict(schema_version=STATEMENT_SCHEMA_VERSION, context=dict(context), totals=totals),
			*ordered,
		):
			encoded = canonical_json(dict(value)).encode("utf-8")
			digest.update(len(encoded).to_bytes(8, "big"))
			digest.update(encoded)
	return CompleteStatement(
		MappingProxyType(dict(context)),
		tuple(MappingProxyType(dict(row)) for row in ordered),
		tuple(rows),
		MappingProxyType(totals),
		digest.hexdigest(),
	)
