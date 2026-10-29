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


def verify_authorized_released_reservation(
	root, event_rows, confirmation_row, decision_row, releases, *, current_facts, renewals=(), commitments=()
):
	"""Verify the frozen root and all independent release/optional Commit pairs.

	This supplied-data contract does not discover or lock database rows. Callers
	must read all physical events and posted independent decisions, authenticate their
	immutable storage, recheck source rights, and acquire all shared guards.
	"""
	from medops.common.budget_reservation_events import MAX_RESERVATION_EVENTS
	from medops.common.reservation_release import verify_released_reservation_records

	if not isinstance(event_rows, (tuple, list)) or not 1 <= len(event_rows) <= MAX_RESERVATION_EVENTS:
		raise ValueError("complete bounded reservation history required")
	if not isinstance(renewals, (tuple, list)):
		raise ValueError("complete original renewal evidence required")
	if not isinstance(commitments, (tuple, list)):
		raise ValueError("complete independent commitment evidence required")
	first = reservation_record_from_row(event_rows[0])
	if first.renewal_decision is not None:
		_, comparison, decision = verify_renewed_initial_reservation(
			root, event_rows[:1], confirmation_row, decision_row, renewals, current_facts=current_facts
		)
	else:
		if renewals:
			raise ValueError("original event cannot have a renewal successor")
		_, comparison, decision = verify_authorized_initial_reservation(
			root, event_rows[:1], confirmation_row, decision_row, current_facts=current_facts
		)
	item, _ = verify_reservation_authorization_rows(confirmation_row, decision_row)
	records = [reservation_record_from_row(row) for row in event_rows]
	if commitments:
		from medops.common.reservation_commitment_history import verify_reservation_action_records

		position = verify_reservation_action_records(item, records, releases, commitments)
	else:
		position = verify_released_reservation_records(item, records, releases)
	return position, comparison, decision


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
