"""Synthetic structural evidence only; never a real person or authority to write."""

from __future__ import annotations

import hashlib

from medops.common.budget_control import budget_control_key
from medops.common.budget_reservation_events import ReservationEvent
from medops.common.budget_reservation_proof import (
	ReservationSource,
	reservation_event_key,
	reservation_record,
)
from medops.common.commitment_evidence import commitment_confirmation, commitment_decision
from medops.common.material_source_snapshot import material_source_snapshot
from medops.common.reservation_authorization import reservation_intent
from medops.common.reservation_commitment import purchase_order_snapshot, reservation_commitment_intent

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
	name = original.reservation_identity
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
