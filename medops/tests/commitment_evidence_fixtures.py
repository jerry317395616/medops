"""Synthetic structural evidence only; never a real person or authority to write."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from medops.common.budget_control import budget_control_key
from medops.common.budget_reservation_events import ReservationEvent
from medops.common.budget_reservation_proof import (
	ReservationSource,
	reservation_event_key,
	reservation_record,
)
from medops.common.commitment_evidence import commitment_confirmation, commitment_decision
from medops.common.domain_service import fingerprint_json
from medops.common.material_source_snapshot import material_source_snapshot, parse_material_source_snapshot
from medops.common.reservation_authorization import reservation_intent
from medops.common.reservation_commitment import purchase_order_snapshot, reservation_commitment_intent
from medops.common.reservation_commitment_history import commitment_record

ACTOR = "manager@example.com"
PROOF = "synthetic_commitment_proof_1234567890"
SESSION = "b" * 64
START = "2026-01-02 11:01:00.000000"
END = "2026-01-02 11:06:00.000000"
DECIDED = "2026-01-02 11:02:00.000000"


def commitment_fixture(*, amount="6.00", order_changes=None):
	material, material_digest = material_source_snapshot(
		dict(
			request="MR-001",
			line="MR-LINE-001",
			request_modified="2026-01-01 09:00:00",
			line_modified="2026-01-01 09:00:00",
			company="Company",
			hospital="Hospital",
			organization_unit="Unit",
			organization_version="Version",
			mapping_revision=1,
			cost_center="Center",
			currency="CNY",
			transaction_date="2026-01-01",
			docstatus=1,
			status="Pending",
			item_code="Item",
			uom="Unit",
			expense_account="Account",
			project=None,
			quantity="5.000000000",
			rate="6.000000000",
			amount="30.000000000",
		)
	)
	key = budget_control_key(
		dict(
			company="Company",
			hospital="Hospital",
			organization_unit="Unit",
			fiscal_year="FY-2026",
			budget_subject="Equipment",
			funding_source="Own Funds",
			project=None,
			period_start="2026-01-01",
			period_end="2026-01-31",
		)
	)
	original = reservation_intent(
		source_snapshot=material,
		source_snapshot_digest=material_digest,
		allocation="allocation-a",
		key=key,
		currency="CNY",
		precision=2,
		amount="10.00",
		budget_date="2026-01-01",
		reason="Synthetic original purchase review",
	)
	name = fingerprint_json(
		{"namespace": "budget-reservation-root-v1", "source_identity": original.reservation_identity}
	)
	event = ReservationEvent(reservation_event_key(name, 1), name, 1, key, "CNY", 2, "Reserve", "10.00")
	record = reservation_record(
		event,
		ReservationSource(
			"Material Request", "MR-001", material_digest, "MR-LINE-001", "allocation-a", "Reserve"
		),
		actor=ACTOR,
		occurred_at="2026-01-02 11:00:00.000000",
		previous_digest=None,
	)
	order, order_digest = purchase_order_snapshot(
		dict(
			order="PO-001",
			line="PO-LINE-001",
			order_modified="2026-01-02 10:00:00",
			line_modified="2026-01-02 10:00:00",
			material_request="MR-001",
			material_request_line="MR-LINE-001",
			material_request_digest=material_digest,
			company="Company",
			hospital="Hospital",
			organization_unit="Unit",
			cost_center="Center",
			expense_account="Account",
			project=None,
			supplier="Supplier",
			currency="CNY",
			transaction_date="2026-01-02",
			status="To Receive and Bill",
			item_code="Item",
			uom="Unit",
			quantity="2.000000000",
			rate="6.000000000",
			amount="12.000000000",
			base_amount="12.000000000",
		)
		| (order_changes or {}),
		material_snapshot=material,
		material_digest=material_digest,
	)
	item = reservation_commitment_intent(
		original,
		[record],
		order_snapshot=order,
		order_digest=order_digest,
		material_snapshot=material,
		material_digest=material_digest,
		amount=amount,
		reason="Synthetic independent commitment review",
	)
	return item, material, order


def evidence_fixture():
	item, material, order = commitment_fixture()
	confirmed = commitment_confirmation(
		item,
		actor=ACTOR,
		proof_hash=hashlib.sha256(PROOF.encode()).hexdigest(),
		session_digest=SESSION,
		recorded_at=START,
		expires_at=END,
	)
	decided = commitment_decision(
		item,
		confirmed,
		proof=PROOF,
		actor=ACTOR,
		session_digest=SESSION,
		recorded_at=DECIDED,
		acknowledge=True,
	)
	return item, confirmed, decided, material, order


def history_commitment(original, records, *, amount="4.00", order_changes=None):
	"""Supply synthetic historical pairs for pure/private-table tests only."""
	material = parse_material_source_snapshot(
		original.source_snapshot, expected_digest=original.source_snapshot_digest
	)
	start = max(
		datetime.fromisoformat(records[-1].occurred_at),
		*(datetime.fromisoformat(material[n]) for n in ("request_modified", "line_modified")),
	) + timedelta(minutes=1)

	def stamp(at):
		return at.isoformat(sep=" ", timespec="microseconds")

	order_facts = dict(
		order="PO-001",
		line="PO-LINE-001",
		order_modified=stamp(start),
		line_modified=stamp(start),
		material_request=material["request"],
		material_request_line=material["line"],
		material_request_digest=original.source_snapshot_digest,
		company=material["company"],
		hospital=material["hospital"],
		organization_unit=material["organization_unit"],
		cost_center=material["cost_center"],
		expense_account=material["expense_account"],
		project=material["project"],
		supplier="Supplier",
		currency=material["currency"],
		transaction_date=original.budget_date,
		status="To Receive and Bill",
		item_code=material["item_code"],
		uom=material["uom"],
		quantity=material["quantity"],
		rate=material["rate"],
		amount=material["amount"],
		base_amount=material["amount"],
	) | (order_changes or {})
	order, digest = purchase_order_snapshot(
		order_facts,
		material_snapshot=original.source_snapshot,
		material_digest=original.source_snapshot_digest,
	)
	item = reservation_commitment_intent(
		original,
		records,
		order_snapshot=order,
		order_digest=digest,
		material_snapshot=original.source_snapshot,
		material_digest=original.source_snapshot_digest,
		amount=amount,
		reason="Synthetic historical commitment",
	)
	confirmed = commitment_confirmation(
		item,
		actor=ACTOR,
		proof_hash=hashlib.sha256(PROOF.encode()).hexdigest(),
		session_digest=SESSION,
		recorded_at=stamp(start),
		expires_at=stamp(start + timedelta(minutes=5)),
	)
	decided = commitment_decision(
		item,
		confirmed,
		proof=PROOF,
		actor=ACTOR,
		session_digest=SESSION,
		recorded_at=stamp(start + timedelta(seconds=1)),
		acknowledge=True,
	)
	from medops.common.commitment_evidence import commitment_evidence_row

	first = commitment_evidence_row(
		item, confirmed, material_snapshot=original.source_snapshot, order_snapshot=order
	)
	second = commitment_evidence_row(
		item, decided, material_snapshot=original.source_snapshot, order_snapshot=order, confirmed=confirmed
	)
	record = commitment_record(
		item,
		confirmed,
		decided,
		records[-1],
		material_snapshot=original.source_snapshot,
		order_snapshot=order,
		occurred_at=stamp(start + timedelta(seconds=2)),
	)
	return item, (first, second), record
