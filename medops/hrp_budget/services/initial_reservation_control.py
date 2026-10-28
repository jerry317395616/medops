"""Transaction-local controller gate for one internal initial reservation."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass

import frappe
from frappe.utils import now_datetime

from medops.common.budget_ledger import LEDGER
from medops.common.initial_reservation_posting import (
	InitialReservationDocuments,
	initial_reservation_documents,
)
from medops.common.material_source_snapshot import material_source_snapshot
from medops.common.reservation_authorization import (
	AUTHORIZATION_ROW_FIELDS,
	verify_reservation_authorization_rows,
	verify_reservation_confirmation_proof,
	verify_reservation_confirmation_row,
)
from medops.hrp_budget.services.approval import session_digest
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.material_budget_guards import assert_material_budget_guards
from medops.hrp_budget.services.material_request_source import (
	MaterialRequestSource,
	locked_material_request_source,
)
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.release_documents import same_value
from medops.services.data_access import require_scope
from medops.services.errors import raise_ione_error, require_roles

ROOT = "HRP Budget Reservation"
EVENT = "HRP Budget Reservation Event"
_active: ContextVar[InitialWrite | None] = ContextVar("medops_initial_reservation_write", default=None)


@dataclass(slots=True)
class InitialWrite:
	documents: InitialReservationDocuments
	source: MaterialRequestSource
	keys: tuple
	root_inserted: bool = False
	root_submitted: bool = False
	event_inserted: bool = False


def _enabled():
	if frappe.conf.get("medops_internal_reservation_write_enabled") is not True:
		raise_ione_error("OPERATION_NOT_ALLOWED")


@contextmanager
def expected_initial_reservation(
	documents: InitialReservationDocuments,
	source: MaterialRequestSource,
	keys: tuple,
	*,
	confirmation: str,
	decision: str,
	proof: str,
) -> Iterator[InitialWrite]:
	"""Limit controllers to one complete exact write, never authorizing its caller.

	The caller must independently authenticate the decision, revalidate source,
	permissions and budget and own the entire transaction. A failure poisons the
	guard; the caller must roll back the whole transaction.
	"""
	try:
		_enabled()
		if _active.get() is not None or type(documents) is not InitialReservationDocuments:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if (
			type(source) is not MaterialRequestSource
			or documents.root.get("created_by") != frappe.session.user
		):
			raise_ione_error("PERMISSION_DENIED")
		assert_material_budget_guards(source, keys)
		require_roles(MANAGER_ROLES)
		for doctype, action in ((ROOT, "write"), (LEDGER, "read")):
			require_scope(
				doctype,
				company=source.company,
				hospital=source.hospital,
				organization_unit=source.organization_unit,
				action=action,
			)
		if locked_material_request_source(source.request, source.line, source.organization_unit) != source:
			raise_ione_error("CONFLICT")
		rows = []
		for name in (confirmation, decision):
			doc = frappe.get_doc("HRP Budget Reservation Authorization", name, for_update=True)
			rows.append({field: doc.get(field) for field in AUTHORIZATION_ROW_FIELDS})
		item, confirmed = verify_reservation_confirmation_row(rows[0])
		checked, decided = verify_reservation_authorization_rows(*rows)
		session = session_digest("budget-reservation:" + item.reservation_identity)
		verify_reservation_confirmation_proof(
			item,
			confirmed,
			proof=proof,
			actor=frappe.session.user,
			session_digest=session,
			now=now_datetime().isoformat(sep=" ", timespec="microseconds"),
		)
		if (
			checked != item
			or decided.actor != frappe.session.user
			or keys != (item.key,)
			or material_source_snapshot(asdict(source)) != (item.source_snapshot, item.source_snapshot_digest)
			or initial_reservation_documents(*rows, occurred_at=documents.event.get("occurred_at"))
			!= documents
		):
			raise_ione_error("CONFLICT")
		state = InitialWrite(documents, source, keys)
		token = _active.set(state)
		try:
			yield state
			if not (state.root_inserted and state.root_submitted and state.event_inserted):
				raise_ione_error("OPERATION_NOT_ALLOWED")
			assert_material_budget_guards(source, keys)
		finally:
			_active.reset(token)
	except BaseException:
		invalidate_budget_guards()
		raise


def require_initial_write(doc, *, doctype: str, stage: str, named: bool = True) -> InitialWrite:
	_enabled()
	state = _active.get()
	if (
		state is None
		or doc.doctype != doctype
		or frappe.session.user != state.documents.root.get("created_by")
	):
		raise_ione_error("OPERATION_NOT_ALLOWED")
	assert_material_budget_guards(state.source, state.keys)
	if doctype == ROOT:
		expected: Mapping[str, object] = state.documents.root
		allowed = (
			stage == "insert" and not state.root_inserted and int(doc.docstatus) == 0 and doc.is_new()
		) or (
			stage == "submit"
			and state.root_inserted
			and not state.root_submitted
			and int(doc.docstatus) == 1
			and not doc.is_new()
		)
	elif doctype == EVENT:
		expected = state.documents.event
		allowed = (
			stage == "insert"
			and state.root_submitted
			and not state.event_inserted
			and int(doc.docstatus) == 0
			and doc.is_new()
		)
	else:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	if not allowed or (named and doc.name != expected["name"]):
		raise_ione_error("OPERATION_NOT_ALLOWED")
	for field in doc.meta.fields:
		if field.fieldtype in {"Section Break", "Column Break", "Tab Break"}:
			continue
		if not same_value(field, doc.get(field.fieldname), expected.get(field.fieldname)):
			raise_ione_error("CONFIGURATION_INVALID")
	return state
