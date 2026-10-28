"""Transaction-local gate for one exact reservation renewal decision."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType

import frappe
from frappe.utils import now_datetime

from medops.common.confirmation_privacy import ConfirmationProof
from medops.common.reservation_authorization import (
	AUTHORIZATION_ROW_FIELDS,
	ReservationConfirmation,
	ReservationIntent,
	verify_reservation_confirmation_row,
)
from medops.common.reservation_renewal import reservation_renewal_decision, reservation_renewal_row
from medops.hrp_budget.services.approval import require_web_session, session_digest
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.material_budget_guards import assert_material_budget_guards
from medops.hrp_budget.services.material_request_source import MaterialRequestSource
from medops.hrp_budget.services.release_documents import same_value
from medops.hrp_budget.services.reservation_confirmation import require_reservation_actor_scope
from medops.hrp_budget.services.reservation_renewal_lineage import locked_renewal_lineage
from medops.services.errors import raise_ione_error

DOCTYPE = "HRP Budget Reservation Renewal"
ROOT = "HRP Budget Reservation"
AUTHORIZATION = "HRP Budget Reservation Authorization"
_active: ContextVar[RenewalWrite | None] = ContextVar("medops_reservation_renewal_write", default=None)


@dataclass(slots=True)
class RenewalWrite:
	values: Mapping[str, object]
	item: ReservationIntent
	source: MaterialRequestSource
	actor: str
	session: str
	confirmation: ReservationConfirmation
	proof: str = field(repr=False)
	inserted: bool = False


def enabled():
	if frappe.conf.get("medops_internal_reservation_renewal_enabled") is not True:
		raise_ione_error("OPERATION_NOT_ALLOWED")


def require_unposted(item):
	if frappe.db.get_value(ROOT, {"source_identity_key": item.reservation_identity}, "name", for_update=True):
		raise_ione_error("CONFLICT")


def read_confirmation(name, *, lock=False):
	doc = frappe.get_doc(AUTHORIZATION, name, for_update=lock)
	return verify_reservation_confirmation_row({field: doc.get(field) for field in AUTHORIZATION_ROW_FIELDS})


def _timestamp():
	return now_datetime().isoformat(sep=" ", timespec="microseconds")


@contextmanager
def expected_reservation_renewal(
	item, source, confirmation_name, original_name, renewal_confirmation_name, proof, *, acknowledge
) -> Iterator[RenewalWrite]:
	"""Independently bind exact evidence to the current session and held guard."""
	try:
		enabled()
		require_web_session()
		if _active.get() is not None or acknowledge is not True:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		proof = ConfirmationProof(proof)
		actor = frappe.session.user
		session = session_digest("budget-reservation:" + item.reservation_identity)
		require_reservation_actor_scope(item, actor, session)
		assert_material_budget_guards(source, [item.key])
		require_unposted(item)
		lineage = locked_renewal_lineage(item, source, confirmation_name, original_name)
		checked, confirmed = read_confirmation(renewal_confirmation_name, lock=True)
		if checked != item:
			raise_ione_error("CONFLICT")
		decision = reservation_renewal_decision(
			item,
			lineage.confirmation,
			lineage.original,
			lineage.renewals,
			confirmed,
			proof=proof,
			actor=actor,
			session_digest=session,
			recorded_at=_timestamp(),
			acknowledge=True,
		)
		values = reservation_renewal_row(
			item, lineage.confirmation, lineage.original, lineage.renewals, confirmed, decision
		)
		state = RenewalWrite(MappingProxyType(values), item, source, actor, session, confirmed, proof)
		token = _active.set(state)
		try:
			yield state
			if not state.inserted:
				raise_ione_error("OPERATION_NOT_ALLOWED")
			assert_material_budget_guards(source, [item.key])
			require_unposted(item)
		finally:
			_active.reset(token)
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise


def require_renewal_write(doc, *, named=True):
	"""Reject controller bypass and poison the guard even after a caught denial."""
	try:
		enabled()
		state = _active.get()
		if (
			state is None
			or state.inserted
			or doc.doctype != DOCTYPE
			or not doc.is_new()
			or int(doc.docstatus) != 0
			or (named and doc.name != state.values["name"])
		):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		require_web_session()
		if frappe.session.user != state.actor:
			raise_ione_error("PERMISSION_DENIED")
		require_reservation_actor_scope(state.item, state.actor, state.session)
		assert_material_budget_guards(state.source, [state.item.key])
		from medops.common.reservation_authorization import verify_reservation_confirmation_proof

		verify_reservation_confirmation_proof(
			state.item,
			state.confirmation,
			proof=state.proof,
			actor=state.actor,
			session_digest=state.session,
			now=_timestamp(),
		)
		for field_meta in doc.meta.fields:
			if field_meta.fieldtype in {"Section Break", "Column Break", "Tab Break"}:
				continue
			if field_meta.fieldname not in state.values or not same_value(
				field_meta, doc.get(field_meta.fieldname), state.values[field_meta.fieldname]
			):
				raise_ione_error("CONFIGURATION_INVALID")
		return state
	except (ValueError, TypeError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
