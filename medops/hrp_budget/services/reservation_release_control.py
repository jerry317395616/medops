"""Transaction-local gates for independent release confirmation and atomic posting."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType

import frappe
from frappe.utils import now_datetime

from medops.common.budget_reservation_proof import ReservationRecord, verify_reservation_records
from medops.common.budget_reservation_storage import reservation_record_row
from medops.common.confirmation_privacy import ConfirmationProof
from medops.common.reservation_release import (
	RELEASE_ROW_FIELDS,
	ReservationReleaseEvidence,
	ReservationReleaseIntent,
	reservation_release_decision,
	reservation_release_intent,
	reservation_release_record,
	reservation_release_row,
	verify_reservation_release_proof,
	verify_reservation_release_row,
)
from medops.hrp_budget.services.approval import require_web_session, session_digest
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.initial_reservation_write import check_persisted_document
from medops.hrp_budget.services.release_documents import same_value
from medops.hrp_budget.services.reservation_release_state import (
	LockedReservationReleaseState,
	check_release_state_access,
	recheck_reservation_release,
)
from medops.services.errors import raise_ione_error

DOCTYPE = "HRP Budget Reservation Release"
EVENT = "HRP Budget Reservation Event"
_active: ContextVar[ReleaseWrite | None] = ContextVar("medops_reservation_release_write", default=None)


@dataclass(slots=True)
class ReleaseWrite:
	history: LockedReservationReleaseState = field(repr=False)
	item: ReservationReleaseIntent = field(repr=False)
	confirmation: ReservationReleaseEvidence = field(repr=False)
	proof: str = field(repr=False)
	values: Mapping[str, object] = field(repr=False)
	event_values: Mapping[str, object] | None = field(default=None, repr=False)
	decision: ReservationReleaseEvidence | None = field(default=None, repr=False)
	record: ReservationRecord | None = field(default=None, repr=False)
	evidence_inserted: bool = False
	event_inserted: bool = False


def enabled():
	if frappe.conf.get("medops_internal_reservation_release_write_enabled") is not True:
		raise_ione_error("OPERATION_NOT_ALLOWED")


def release_write_active():
	return _active.get() is not None


def release_session(reservation):
	return session_digest("budget-reservation-release:" + reservation)


def timestamp():
	return now_datetime().isoformat(sep=" ", timespec="microseconds")


def read_release_confirmation(name, *, lock=False):
	doc = frappe.get_doc(DOCTYPE, name, for_update=lock)
	item, confirmed = verify_reservation_release_row({name: doc.get(name) for name in RELEASE_ROW_FIELDS})
	if confirmed.event_kind != "Confirmation":
		raise ValueError("independent release confirmation required")
	check_persisted_document(doc, reservation_release_row(item, confirmed), 0)
	return item, confirmed


def check_proof(item, confirmed, proof, *, at=None):
	require_web_session()
	try:
		verify_reservation_release_proof(
			item,
			confirmed,
			proof=proof,
			actor=frappe.session.user,
			session_digest=release_session(item.reservation),
			now=timestamp() if at is None else at,
		)
	except ValueError as error:
		raise_ione_error("PERMISSION_DENIED", cause=error)


@contextmanager
def expected_reservation_release(history, item, confirmed, proof, *, post=False, acknowledge=False):
	"""Re-read independent authority; allow exactly one confirmation or decision/event pair."""
	try:
		from medops.hrp_budget.services.initial_reservation_control import initial_write_active
		from medops.hrp_budget.services.reservation_commitment_confirmation import (
			commitment_confirmation_active,
		)
		from medops.hrp_budget.services.reservation_commitment_control import commitment_post_active

		enabled()
		if (
			_active.get() is not None
			or initial_write_active()
			or commitment_confirmation_active()
			or commitment_post_active()
			or type(post) is not bool
		):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		proof = ConfirmationProof(proof)
		checked_history = recheck_reservation_release(history)
		if (
			reservation_release_intent(
				checked_history.original,
				checked_history.position,
				checked_history.records[-1],
				amount=item.amount,
				reason=item.reason,
			)
			!= item
		):
			raise ValueError("release confirmation no longer matches current history")
		check_proof(item, confirmed, proof)
		decision = record = event_values = None
		if post:
			stored_item, stored = read_release_confirmation(confirmed.name, lock=True)
			if stored_item != item or stored != confirmed or acknowledge is not True:
				raise_ione_error("CONFLICT")
			decision = reservation_release_decision(
				item,
				confirmed,
				proof=proof,
				actor=frappe.session.user,
				session_digest=release_session(item.reservation),
				recorded_at=timestamp(),
				acknowledge=True,
			)
			record = reservation_release_record(
				item, confirmed, decision, checked_history.records[-1], occurred_at=timestamp()
			)
			event_values = MappingProxyType(reservation_record_row(record))
		values = (
			reservation_release_row(item, decision, confirmed)
			if post
			else reservation_release_row(item, confirmed)
		)
		state = ReleaseWrite(
			checked_history, item, confirmed, proof, MappingProxyType(values), event_values, decision, record
		)
		token = _active.set(state)
		try:
			yield state
			if not state.evidence_inserted or (post and not state.event_inserted):
				raise_ione_error("OPERATION_NOT_ALLOWED")
			check_proof(item, confirmed, proof)
			position = verify_reservation_records([*checked_history.records, record]) if post else None
			recheck_reservation_release(checked_history, position=position)
		finally:
			_active.reset(token)
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise


def require_reservation_release_write(doc, *, doctype=DOCTYPE, named=True):
	"""Each controller stage checks exact fields, actor/source access and proof expiry."""
	try:
		enabled()
		state = _active.get()
		if state is None or doc.doctype != doctype or not doc.is_new() or int(doc.docstatus) != 0:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if doctype == DOCTYPE:
			if state.evidence_inserted:
				raise_ione_error("OPERATION_NOT_ALLOWED")
			values = state.values
		elif doctype == EVENT:
			if not state.evidence_inserted or state.event_inserted or state.event_values is None:
				raise_ione_error("OPERATION_NOT_ALLOWED")
			values = state.event_values
		else:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		check_release_state_access(state.history)
		check_proof(state.item, state.confirmation, state.proof)
		if state.decision is not None:
			item, confirmed = read_release_confirmation(state.confirmation.name, lock=True)
			if item != state.item or confirmed != state.confirmation:
				raise_ione_error("CONFLICT")
		if named and doc.name != values["name"]:
			raise_ione_error("CONFIGURATION_INVALID")
		for meta in doc.meta.fields:
			if meta.fieldtype in {"Section Break", "Column Break", "Tab Break"}:
				continue
			if meta.fieldname not in values or not same_value(
				meta, doc.get(meta.fieldname), values[meta.fieldname]
			):
				raise_ione_error("CONFIGURATION_INVALID")
		return state
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
