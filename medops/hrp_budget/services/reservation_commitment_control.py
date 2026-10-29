"""Exact transaction-local Decision and Commit appends, default disabled."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, localcontext
from types import MappingProxyType

import frappe

from medops.common.budget_reservation_proof import ReservationRecord, verify_reservation_records
from medops.common.budget_reservation_storage import reservation_record_row
from medops.common.commitment_evidence import (
	COMMITMENT_ROW_FIELDS,
	CommitmentEvidence,
	commitment_decision,
	commitment_evidence_row,
	verify_commitment_evidence_row,
)
from medops.common.confirmation_privacy import ConfirmationProof
from medops.common.exact_money import exact_decimal_amount
from medops.common.material_source_snapshot import material_source_snapshot
from medops.common.reservation_commitment_history import commitment_record
from medops.hrp_budget.services import reservation_commitment_confirmation as confirmation
from medops.hrp_budget.services.commitment_authorization import DOCTYPE
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.initial_reservation_control import initial_write_active
from medops.hrp_budget.services.initial_reservation_write import check_persisted_document
from medops.hrp_budget.services.material_usage_preview import PurchaseUsageInspection
from medops.hrp_budget.services.material_usage_sources import (
	guarded_verify_provisional_material_usage_sources,
)
from medops.hrp_budget.services.release_documents import same_value
from medops.hrp_budget.services.reservation_commitment_allocations import (
	commitment_decision_set_digest,
	read_guarded_commitment_records,
	verify_guarded_purchase_allocations,
)
from medops.hrp_budget.services.reservation_commitment_state import (
	LockedCommitmentState,
	check_commitment_state_access,
	recheck_reservation_commitment,
)
from medops.hrp_budget.services.reservation_release_control import release_write_active
from medops.services.errors import raise_ione_error

EVENT = "HRP Budget Reservation Event"
_active: ContextVar[CommitmentPostWrite | None] = ContextVar("medops_commitment_post_write", default=None)


@dataclass(slots=True)
class CommitmentPostWrite:
	state: LockedCommitmentState = field(repr=False)
	confirmed: CommitmentEvidence = field(repr=False)
	proof: str = field(repr=False)
	decision: CommitmentEvidence = field(repr=False)
	record: ReservationRecord = field(repr=False)
	records: tuple[ReservationRecord, ...] = field(repr=False)
	values: Mapping[str, object] = field(repr=False)
	event_values: Mapping[str, object] = field(repr=False)
	documents: dict[str, object] = field(default_factory=dict, repr=False)
	evidence_inserted: bool = False
	event_inserted: bool = False
	posted_inspection: PurchaseUsageInspection | None = field(default=None, repr=False)


def commitment_post_active():
	return _active.get() is not None


def enabled():
	confirmation.enabled()
	if frappe.conf.get("medops_internal_commitment_write_enabled") is not True:
		raise_ione_error("OPERATION_NOT_ALLOWED")


def recheck_posted_commitment(write):
	"""Authenticate the whole new history and exactly one additional global allocation."""
	state = write.state
	check_commitment_state_access(state)
	expected_records = (*write.records, write.record)
	if (
		read_guarded_commitment_records(
			state.inspection.keys, state.item.reservation, state.item.previous_sequence + 1
		)
		!= expected_records
	):
		raise ValueError("actual complete Commit history differs from exact append")
	position = verify_reservation_records(expected_records)
	current = guarded_verify_provisional_material_usage_sources(
		state.inspection.keys, state.inspection.sources, discovered=state.inspection.discovered
	)
	expected = tuple(
		position if item.reservation == state.item.reservation else item for item in state.inspection.usage
	)
	if current != expected:
		raise ValueError("Commit changed an unrelated budget position or source membership")
	selected = {item.reservation for item in state.inspection.source_usage}
	inspection = replace(
		state.inspection, usage=current, source_usage=tuple(p for p in current if p.reservation in selected)
	)
	capacity, pairs = verify_guarded_purchase_allocations(inspection, state.purchase)
	new = [pair for pair in pairs if pair[2].name == write.decision.name]
	old = tuple(pair for pair in pairs if pair[2].name != write.decision.name)
	if (
		len(new) != 1
		or new[0][:3] != (state.item, write.confirmed, write.decision)
		or capacity.decision_count != state.capacity.decision_count + 1
		or commitment_decision_set_digest(old) != state.capacity.decision_set_digest
	):
		raise ValueError("global purchase allocation set differs from exact Commit append")
	with localcontext() as context:
		context.prec = 48
		if capacity.committed != exact_decimal_amount(
			Decimal(state.capacity.committed) + Decimal(state.item.amount), 9
		) or capacity.remaining != exact_decimal_amount(
			Decimal(state.capacity.remaining) - Decimal(state.item.amount), 9
		):
			raise ValueError("posted global purchase capacity differs from exact conversion")
	check_commitment_state_access(state)
	write.posted_inspection = inspection
	return inspection


@contextmanager
def expected_commitment_post(state, confirmed, proof, *, acknowledge):
	"""Permit one exact Decision then one exact Commit event; caller owns rollback."""
	try:
		enabled()
		if (
			commitment_post_active()
			or confirmation.commitment_confirmation_active()
			or initial_write_active()
			or release_write_active()
			or acknowledge is not True
		):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		state = recheck_reservation_commitment(state)
		proof = ConfirmationProof(proof)
		confirmation.check_commitment_confirmation_proof(state, confirmed, proof)
		item, persisted, doc = confirmation.read_commitment_confirmation(confirmed.name, lock=True)
		if item != state.item or persisted != confirmed or doc.owner != state.actor:
			raise_ione_error("CONFLICT")
		material, _ = material_source_snapshot(asdict(state.inspection.source))
		order, _ = state.purchase.commitment_snapshot(state.inspection.source)
		check_persisted_document(
			doc, commitment_evidence_row(item, confirmed, material_snapshot=material, order_snapshot=order), 0
		)
		records = read_guarded_commitment_records(
			state.inspection.keys, item.reservation, item.previous_sequence
		)
		decision = commitment_decision(
			item,
			confirmed,
			proof=proof,
			actor=state.actor,
			session_digest=state.session,
			recorded_at=confirmation.timestamp(),
			acknowledge=True,
		)
		record = commitment_record(
			item,
			confirmed,
			decision,
			records[-1],
			material_snapshot=material,
			order_snapshot=order,
			occurred_at=confirmation.timestamp(),
		)
		write = CommitmentPostWrite(
			state,
			confirmed,
			proof,
			decision,
			record,
			records,
			MappingProxyType(
				commitment_evidence_row(
					item, decision, material_snapshot=material, order_snapshot=order, confirmed=confirmed
				)
			),
			MappingProxyType(reservation_record_row(record)),
		)
		token = _active.set(write)
		try:
			yield write
			if not write.evidence_inserted or not write.event_inserted:
				raise_ione_error("OPERATION_NOT_ALLOWED")
			for doctype, values in ((DOCTYPE, write.values), (EVENT, write.event_values)):
				stored = frappe.get_doc(doctype, values["name"], for_update=True)
				check_persisted_document(stored, values, 0)
				if stored.owner != state.actor:
					raise_ione_error("CONFLICT")
			stored = frappe.get_doc(DOCTYPE, decision.name, for_update=True)
			if verify_commitment_evidence_row(
				{key: stored.get(key) for key in COMMITMENT_ROW_FIELDS}, confirmed
			) != (item, decision):
				raise_ione_error("CONFLICT")
			confirmation.check_commitment_confirmation_proof(state, confirmed, proof)
			recheck_posted_commitment(write)
		finally:
			_active.reset(token)
	except (TypeError, ValueError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise


def require_commitment_post_write(doc, *, doctype=DOCTYPE, named=True):
	"""Require the fixed document object and exact canonical values at every stage."""
	try:
		enabled()
		write = _active.get()
		if write is None or doc.doctype != doctype or not doc.is_new() or int(doc.docstatus) != 0:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if doctype == DOCTYPE and not write.evidence_inserted:
			values = write.values
		elif doctype == EVENT and write.evidence_inserted and not write.event_inserted:
			values = write.event_values
		else:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if doctype not in write.documents:
			write.documents[doctype] = doc
		elif write.documents[doctype] is not doc:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if doc.owner != write.state.actor or (named and doc.name != values["name"]):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		for meta in doc.meta.fields:
			if meta.fieldtype in {"Section Break", "Column Break", "Tab Break"}:
				continue
			if meta.fieldname not in values or not same_value(
				meta, doc.get(meta.fieldname), values[meta.fieldname]
			):
				raise_ione_error("CONFIGURATION_INVALID")
		check_commitment_state_access(write.state)
		confirmation.check_commitment_confirmation_proof(write.state, write.confirmed, write.proof)
		item, confirmed, doc = confirmation.read_commitment_confirmation(write.confirmed.name, lock=True)
		if (item, confirmed, doc.owner) != (write.state.item, write.confirmed, write.state.actor):
			raise_ione_error("CONFLICT")
		return write
	except (TypeError, ValueError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
