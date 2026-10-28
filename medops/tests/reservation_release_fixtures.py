"""Pure synthetic release helpers shared by local and installed-app tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from medops.common.budget_reservation_proof import verify_reservation_records
from medops.common.reservation_release import (
	reservation_release_confirmation,
	reservation_release_decision,
	reservation_release_intent,
	reservation_release_record,
	reservation_release_row,
)

ACTOR = "manager@example.com"
PROOF = "synthetic_confirmation_proof_1234567890"
SESSION = "a" * 64


def stamp(value):
	return value.isoformat(sep=" ", timespec="microseconds")


def release_evidence(original, records, amount: object = "4.00", minute=10):
	position = verify_reservation_records(records)
	item = reservation_release_intent(original, position, records[-1], amount=amount, reason="Unused portion")
	now = datetime(2026, 1, 1, 10, minute)
	confirmed = reservation_release_confirmation(
		item,
		actor=ACTOR,
		proof_hash=hashlib.sha256(PROOF.encode()).hexdigest(),
		session_digest=SESSION,
		recorded_at=stamp(now),
		expires_at=stamp(now + timedelta(minutes=5)),
	)
	decided = reservation_release_decision(
		item,
		confirmed,
		proof=PROOF,
		actor=ACTOR,
		session_digest=SESSION,
		recorded_at=stamp(now + timedelta(seconds=1)),
		acknowledge=True,
	)
	record = reservation_release_record(
		item,
		confirmed,
		decided,
		records[-1],
		occurred_at=stamp(now + timedelta(seconds=2)),
	)
	return item, confirmed, decided, record


def rows(item, first, second):
	return reservation_release_row(item, first), reservation_release_row(item, second, first)
