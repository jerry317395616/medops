"""Bounded, fixed-filter financial directories; no grants or database access."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from medops.common.budget_ledger import RELEASE_PUBLIC_FIELDS
from medops.common.budget_planning import object_payload
from medops.common.domain_service import canonical_json, normalize_sha256
from medops.common.organization import normalize_nonnegative_integer, normalize_reference

MAX_DIRECTORY_CANDIDATES = 1000
MAX_DIRECTORY_PAGE = 50
CONTEXT_FIELDS = frozenset({"company", "hospital", "fiscal_year"})
RECORD_FILTERS = CONTEXT_FIELDS | {
	"organization_unit",
	"currency",
	"name",
	"status",
	"operation",
	"source_approval",
	"reversal_of",
}
SOURCE_FILTERS = CONTEXT_FIELDS | {"organization_unit", "currency", "name"}
SOURCE_PUBLIC_FIELDS = (
	"name",
	"revision",
	"subject",
	"company",
	"hospital",
	"organization_unit",
	"fiscal_year",
	"currency",
	"currency_precision",
	"total_amount",
	"line_count",
	"submission",
)


@dataclass(frozen=True, slots=True)
class ReleaseDirectoryRequest:
	kind: Literal["records", "eligible"]
	filters: tuple[tuple[str, str], ...]
	offset: int
	limit: int
	expected_digest: str | None


def build_release_directory(
	kind, filters, *, offset: object = 0, limit: object = 50, expected_digest: object = None
):
	if kind not in {"records", "eligible"}:
		raise ValueError("unknown financial directory")
	data = object_payload(
		filters if filters is not None else {}, set(RECORD_FILTERS if kind == "records" else SOURCE_FILTERS)
	)
	if not CONTEXT_FIELDS <= set(data):
		raise ValueError("company, hospital and fiscal year are required")
	values = {key: normalize_reference(value, label=key) for key, value in data.items()}
	if values.get("status", "Prepared") not in {"Prepared", "Authorized", "Posted"}:
		raise ValueError("invalid release status")
	if values.get("operation", "Release") not in {"Release", "Reverse"}:
		raise ValueError("invalid release operation")
	start = normalize_nonnegative_integer(offset, label="offset")
	length = normalize_nonnegative_integer(limit, label="limit")
	if start > MAX_DIRECTORY_CANDIDATES or not 1 <= length <= MAX_DIRECTORY_PAGE:
		raise ValueError("financial directory exceeds bounds")
	digest = (
		normalize_sha256(expected_digest, label="expected_digest")
		if expected_digest not in (None, "")
		else None
	)
	if start and not digest:
		raise ValueError("subsequent pages require the visible directory digest")
	return ReleaseDirectoryRequest(kind, tuple(sorted(values.items())), start, length, digest)


def directory_digest(command: ReleaseDirectoryRequest, rows: Sequence[Mapping[str, object]]):
	if len(rows) > MAX_DIRECTORY_CANDIDATES:
		raise ValueError("directory exceeds bounds")
	fields = set(RELEASE_PUBLIC_FIELDS if command.kind == "records" else SOURCE_PUBLIC_FIELDS)
	if any(set(row) != fields for row in rows) or len({row["name"] for row in rows}) != len(rows):
		raise ValueError("only unique reviewed public projections may enter a directory")
	digest = sha256()
	for value in (dict(kind=command.kind, filters=dict(command.filters), schema_version=1), *rows):
		encoded = canonical_json(dict(value)).encode("utf-8")
		digest.update(len(encoded).to_bytes(8, "big"))
		digest.update(encoded)
	return digest.hexdigest()
