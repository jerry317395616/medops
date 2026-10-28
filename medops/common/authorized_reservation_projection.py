"""Bind one initial reservation event to its independently stored decision.

This only checks supplied data. The caller must establish controlled immutable
storage, complete database rows, current source permissions and shared locks.
"""

from __future__ import annotations

from datetime import datetime

from medops.common.budget_reservation_storage import reservation_record_from_row
from medops.common.material_source_snapshot import verify_snapshot_reservation_projection
from medops.common.reservation_authorization import (
	verify_reservation_authorization_rows,
	verify_reservation_confirmation_row,
)
from medops.common.reservation_renewal import verify_reservation_renewal_chain


def verify_authorized_initial_reservation(
	root: object, event_rows: object, confirmation_row: object, decision_row: object, *, current_facts: object
):
	"""Verify a single initial reservation against a checked confirmation pair.

	Later commit, release or actual events are deliberately unsupported until
	their independent authorization and native source adapters are complete.
	A matching hash chain alone never proves that a person or database authorized
	these rows, that the history is complete, or that funds were available.
	"""
	intent, decision = verify_reservation_authorization_rows(confirmation_row, decision_row)
	return _verify_projection(root, event_rows, intent, decision, current_facts=current_facts, renewed=False)


def verify_renewed_initial_reservation(
	root: object,
	event_rows: object,
	confirmation_row: object,
	decision_row: object,
	renewals: object,
	*,
	current_facts: object,
):
	"""Bind a first event to a supplied validated renewal lineage.

	The caller must independently establish immutable and complete database
	lineage, current source authority, shared locks and valid human approval.
	"""
	intent, original = verify_reservation_authorization_rows(confirmation_row, decision_row)
	checked, first = verify_reservation_confirmation_row(confirmation_row)
	if checked != intent or not isinstance(renewals, (tuple, list)) or not renewals:
		raise ValueError("complete renewal evidence required")
	decision = verify_reservation_renewal_chain(intent, first, original, renewals)
	return _verify_projection(root, event_rows, intent, decision, current_facts=current_facts, renewed=True)


def _verify_projection(root, event_rows, intent, decision, *, current_facts, renewed):
	if not isinstance(event_rows, (tuple, list)) or len(event_rows) != 1:
		raise ValueError("only one independently authorized initial event is supported")
	position, comparison = verify_snapshot_reservation_projection(
		root,
		event_rows,
		expected_snapshot_digest=intent.source_snapshot_digest,
		allocation=intent.allocation,
		expected_key=intent.key,
		current_facts=current_facts,
	)
	record = reservation_record_from_row(event_rows[0])
	stamp = datetime.fromisoformat(record.occurred_at)
	if (
		not isinstance(root, dict)
		or root.get("source_snapshot_json") != intent.source_snapshot
		or root.get("source_identity_key") != intent.reservation_identity
		or position.last_sequence != 1
		or position.currency != intent.currency
		or position.precision != intent.precision
		or position.reserved != intent.amount
		or position.open_reservation != intent.amount
		or record.event.kind != "Reserve"
		or record.event.amount != intent.amount
		or record.actor != decision.actor
		or stamp < datetime.fromisoformat(decision.recorded_at)
		or stamp >= datetime.fromisoformat(decision.expires_at)
	):
		raise ValueError("initial reservation differs from its confirmed intent")
	if renewed:
		if record.renewal_decision != decision.name or record.renewal_digest != decision.event_digest:
			raise ValueError("initial event differs from valid renewal decision")
	elif record.renewal_decision is not None or record.renewal_digest is not None:
		raise ValueError("renewed event requires independent renewal evidence")
	return position, comparison, decision
