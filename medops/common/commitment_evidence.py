"""Independent commitment evidence. Structure and proof checks never grant a write."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime

from medops.common.budget_control import KEY_FIELDS
from medops.common.budget_workflow import CONFIRMATION_SECONDS
from medops.common.domain_service import fingerprint_json, normalize_sha256
from medops.common.material_source_snapshot import parse_material_source_snapshot
from medops.common.organization import normalize_reference
from medops.common.reservation_commitment import (
	parse_purchase_order_snapshot,
	parse_reservation_commitment_snapshot,
	require_commitment_intent,
	reservation_commitment_snapshot,
)


def _time(value):
	if not isinstance(value, str):
		raise ValueError("canonical commitment evidence time required")
	try:
		stamp = datetime.fromisoformat(value)
	except ValueError as error:
		raise ValueError("canonical commitment evidence time required") from error
	if stamp.tzinfo is not None or stamp.isoformat(sep=" ", timespec="microseconds") != value:
		raise ValueError("canonical commitment evidence microsecond time required")
	return stamp


def _hash(value, label):
	if normalize_sha256(value, label=label) != value:
		raise ValueError("canonical commitment evidence digest required")
	return value


@dataclass(frozen=True, slots=True)
class CommitmentEvidence:
	name: str
	event_kind: str
	intent_digest: str
	reservation: str
	actor: str
	proof_hash: str = field(repr=False)
	session_digest: str = field(repr=False)
	recorded_at: str
	expires_at: str
	confirmation: str | None
	acknowledged: bool
	one_time_key: str | None
	event_digest: str


def _evidence(item, *, actor, proof_hash, session_digest, recorded_at, expires_at, confirmed=None):
	item = require_commitment_intent(item)
	if normalize_reference(actor, label="actor") != actor:
		raise ValueError("canonical commitment actor required")
	proof, session = _hash(proof_hash, "proof_hash"), _hash(session_digest, "session_digest")
	start, end = _time(recorded_at), _time(expires_at)
	if (
		start < max(_time(item.previous_occurred_at), _time(item.source_modified_at))
		or not 0 < (end - start).total_seconds() <= CONFIRMATION_SECONDS
	):
		raise ValueError("commitment confirmation precedes its source or exceeds its window")
	confirmation = one_time = None
	if confirmed is not None:
		checked = verify_commitment_evidence(item, confirmed)
		if not _time(checked.recorded_at) <= start < end or (actor, proof, session, expires_at) != (
			checked.actor,
			checked.proof_hash,
			checked.session_digest,
			checked.expires_at,
		):
			raise ValueError("commitment decision differs from confirmation or its window")
		confirmation = checked.name
		one_time = fingerprint_json(
			{
				"namespace": "budget-reservation-commitment-successor-v1",
				"reservation": item.reservation,
				"previous_event_digest": item.previous_event_digest,
			}
		)
	item_evidence = CommitmentEvidence(
		name="",
		event_kind="Decision" if confirmed is not None else "Confirmation",
		intent_digest=item.intent_digest,
		reservation=item.reservation,
		actor=actor,
		proof_hash=proof,
		session_digest=session,
		recorded_at=recorded_at,
		expires_at=expires_at,
		confirmation=confirmation,
		acknowledged=confirmed is not None,
		one_time_key=one_time,
		event_digest="",
	)
	values = asdict(item_evidence)
	del values["name"], values["event_digest"]
	name = fingerprint_json({"namespace": "budget-reservation-commitment-evidence-v1", **values})
	return replace(item_evidence, name=name, event_digest=fingerprint_json({"name": name, **values}))


def commitment_confirmation(item, *, actor, proof_hash, session_digest, recorded_at, expires_at):
	return _evidence(
		item,
		actor=actor,
		proof_hash=proof_hash,
		session_digest=session_digest,
		recorded_at=recorded_at,
		expires_at=expires_at,
	)


def verify_commitment_evidence(item, value, confirmed=None):
	if type(value) is not CommitmentEvidence:
		raise ValueError("canonical commitment evidence required")
	expected = _evidence(
		item,
		actor=value.actor,
		proof_hash=value.proof_hash,
		session_digest=value.session_digest,
		recorded_at=value.recorded_at,
		expires_at=value.expires_at,
		confirmed=confirmed,
	)
	if value != expected:
		raise ValueError("commitment evidence differs from its bound facts")
	return expected


def verify_commitment_proof(item, confirmed, *, proof, actor, session_digest, now):
	checked = verify_commitment_evidence(item, confirmed)
	if (
		not isinstance(proof, str)
		or not 32 <= len(proof) <= 128
		or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in proof)
		or actor != checked.actor
		or not hmac.compare_digest(_hash(session_digest, "session_digest"), checked.session_digest)
		or not hmac.compare_digest(hashlib.sha256(proof.encode("ascii")).hexdigest(), checked.proof_hash)
		or not _time(checked.recorded_at) <= _time(now) < _time(checked.expires_at)
	):
		raise ValueError("commitment proof, actor, session or time differs")


def commitment_decision(item, confirmed, *, proof, actor, session_digest, recorded_at, acknowledge):
	if acknowledge is not True:
		raise ValueError("explicit commitment acknowledgement required")
	verify_commitment_proof(
		item, confirmed, proof=proof, actor=actor, session_digest=session_digest, now=recorded_at
	)
	return _evidence(
		item,
		actor=actor,
		proof_hash=confirmed.proof_hash,
		session_digest=session_digest,
		recorded_at=recorded_at,
		expires_at=confirmed.expires_at,
		confirmed=confirmed,
	)


COMMITMENT_ROW_FIELDS = frozenset(CommitmentEvidence.__dataclass_fields__) | {
	"evidence_key",
	"previous_sequence",
	"previous_event_digest",
	"source_modified_at",
	*KEY_FIELDS,
	"budget_key_digest",
	"commitment_identity",
	"purchase_order",
	"purchase_order_line",
	"current_source_digest",
	"order_snapshot_digest",
	"intent_snapshot_json",
	"material_snapshot_json",
	"order_snapshot_json",
	"proof_version",
}


def commitment_sources(item, material_snapshot, order_snapshot):
	material = parse_material_source_snapshot(material_snapshot, expected_digest=item.current_source_digest)
	order = parse_purchase_order_snapshot(
		order_snapshot,
		expected_digest=item.order_snapshot_digest,
		material_snapshot=material_snapshot,
		material_digest=item.current_source_digest,
	)
	modified = max(
		datetime.fromisoformat(value)
		for value in (
			material["request_modified"],
			material["line_modified"],
			order["order_modified"],
			order["line_modified"],
		)
	).isoformat(sep=" ", timespec="microseconds")
	identity = fingerprint_json(
		{"namespace": "budget-purchase-order-commitment-v1", "order": order["order"], "line": order["line"]}
	)
	if (
		(item.key.company, item.key.hospital, item.key.organization_unit, item.key.project, item.currency)
		!= (
			material["company"],
			material["hospital"],
			material["organization_unit"],
			material["project"],
			material["currency"],
		)
		or item.source_modified_at != modified
		or item.commitment_identity != identity
		or item.order_amount != order["amount"]
		or not item.key.period_start <= order["transaction_date"] <= item.key.period_end
	):
		raise ValueError("commitment intent differs from its independent source snapshots")
	return order


def commitment_evidence_row(item, value, *, material_snapshot, order_snapshot, confirmed=None):
	item = require_commitment_intent(item)
	checked = verify_commitment_evidence(item, value, confirmed)
	order = commitment_sources(item, material_snapshot, order_snapshot)
	return {
		**asdict(checked),
		"evidence_key": checked.name,
		"previous_sequence": item.previous_sequence,
		"previous_event_digest": item.previous_event_digest,
		"source_modified_at": item.source_modified_at,
		**asdict(item.key),
		"budget_key_digest": item.key.identity,
		"commitment_identity": item.commitment_identity,
		"purchase_order": order["order"],
		"purchase_order_line": order["line"],
		"current_source_digest": item.current_source_digest,
		"order_snapshot_digest": item.order_snapshot_digest,
		"intent_snapshot_json": reservation_commitment_snapshot(item),
		"material_snapshot_json": material_snapshot,
		"order_snapshot_json": order_snapshot,
		"acknowledged": int(checked.acknowledged),
		"proof_version": 1,
	}


def verify_commitment_evidence_row(value, confirmed=None):
	"""Parse complete stored columns; caller still proves origin and all history."""
	if not isinstance(value, dict) or set(value) != COMMITMENT_ROW_FIELDS:
		raise ValueError("complete commitment evidence row required")
	row = dict(value)
	if (
		type(row["proof_version"]) is not int
		or row["proof_version"] != 1
		or type(row["acknowledged"]) is not int
		or row["acknowledged"] not in (0, 1)
		or type(row["previous_sequence"]) is not int
	):
		raise ValueError("canonical commitment evidence version and flags required")
	for name in ("recorded_at", "expires_at", "source_modified_at"):
		stamp = row[name]
		if isinstance(stamp, datetime) and stamp.tzinfo is None:
			row[name] = stamp.isoformat(sep=" ", timespec="microseconds")
		elif isinstance(stamp, str):
			parsed = datetime.fromisoformat(stamp)
			if parsed.tzinfo is not None or stamp not in (
				parsed.isoformat(sep=" ", timespec="seconds"),
				parsed.isoformat(sep=" ", timespec="microseconds"),
			):
				raise ValueError("canonical persisted commitment time required")
			row[name] = parsed.isoformat(sep=" ", timespec="microseconds")
		else:
			raise ValueError("canonical persisted commitment time required")
	for name in ("period_start", "period_end"):
		if isinstance(row[name], datetime) or not isinstance(row[name], (date, str)):
			raise ValueError("canonical persisted commitment period required")
		if isinstance(row[name], date):
			row[name] = row[name].isoformat()
	for name in ("confirmation", "one_time_key", "project"):
		if row[name] == "":
			row[name] = None
	item = parse_reservation_commitment_snapshot(row["intent_snapshot_json"])
	values = {name: row[name] for name in CommitmentEvidence.__dataclass_fields__}
	values["acknowledged"] = bool(row["acknowledged"])
	evidence = CommitmentEvidence(**values)
	if (
		commitment_evidence_row(
			item,
			evidence,
			material_snapshot=row["material_snapshot_json"],
			order_snapshot=row["order_snapshot_json"],
			confirmed=confirmed,
		)
		!= row
	):
		raise ValueError("stored commitment evidence differs from its bound columns")
	return item, evidence
