"""Bounded source/budget recovery reads; never reissue a proof or authorize posting."""

from __future__ import annotations

from dataclasses import dataclass, field

from medops.common.budget_control import BudgetControlKey, budget_control_key
from medops.common.budget_planning import object_payload
from medops.common.domain_service import normalize_sha256
from medops.common.material_source_snapshot import parse_material_source_snapshot

MAX_RECOVERY_RECORDS = 100
MAX_RECOVERY_PAGE = 20


@dataclass(frozen=True, slots=True)
class ReservationRecoveryQuery:
	source_snapshot: str = field(repr=False)
	source_snapshot_digest: str
	key: BudgetControlKey
	offset: int = 0
	limit: int = MAX_RECOVERY_PAGE
	expected_digest: str | None = None
	decision: str | None = None


def build_reservation_recovery(value: object) -> ReservationRecoveryQuery:
	fields = {
		"source_snapshot",
		"source_snapshot_digest",
		"budget_key",
		"offset",
		"limit",
		"expected_digest",
		"decision",
	}
	data = object_payload(value, fields)
	if not {"source_snapshot", "source_snapshot_digest", "budget_key"} <= set(data):
		raise ValueError("complete recovery source and budget required")
	digest = normalize_sha256(data["source_snapshot_digest"], label="source_snapshot_digest")
	facts = parse_material_source_snapshot(data["source_snapshot"], expected_digest=digest)
	key = budget_control_key(data["budget_key"])
	if (key.company, key.hospital, key.organization_unit, key.project) != (
		facts["company"],
		facts["hospital"],
		facts["organization_unit"],
		facts["project"],
	):
		raise ValueError("recovery source and budget scope differ")
	offset, limit = data.get("offset", 0), data.get("limit", MAX_RECOVERY_PAGE)
	if (
		type(offset) is not int
		or type(limit) is not int
		or not 0 <= offset <= MAX_RECOVERY_RECORDS
		or not 1 <= limit <= MAX_RECOVERY_PAGE
	):
		raise ValueError("bounded integer recovery page required")
	expected = data.get("expected_digest")
	if expected is not None:
		expected = normalize_sha256(expected, label="expected_digest")
	if offset and expected is None:
		raise ValueError("subsequent recovery page requires complete directory digest")
	decision = data.get("decision")
	if decision is not None:
		decision = normalize_sha256(decision, label="decision")
		if offset:
			raise ValueError("targeted recovery cannot be paged")
	return ReservationRecoveryQuery(data["source_snapshot"], digest, key, offset, limit, expected, decision)
