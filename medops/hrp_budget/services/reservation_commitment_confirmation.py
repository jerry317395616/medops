"""Default-disabled, exact standard-controller commitment preview persistence."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from types import MappingProxyType

import frappe
from frappe.utils import now_datetime

from medops.common.budget_workflow import CONFIRMATION_SECONDS
from medops.common.commitment_evidence import (
	COMMITMENT_ROW_FIELDS,
	CommitmentEvidence,
	commitment_confirmation,
	commitment_evidence_row,
	verify_commitment_evidence_row,
	verify_commitment_proof,
)
from medops.common.confirmation_privacy import ConfirmationProof
from medops.common.material_source_snapshot import material_source_snapshot
from medops.hrp_budget.services.commitment_authorization import DOCTYPE, require_commitment_evidence_storage
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.initial_reservation_control import initial_write_active
from medops.hrp_budget.services.initial_reservation_write import check_persisted_document, exact_new_document
from medops.hrp_budget.services.release_documents import same_value
from medops.hrp_budget.services.reservation_commitment_state import (
	LockedCommitmentState,
	check_commitment_state_access,
	commitment_session,
	inspect_reservation_commitment,
	recheck_reservation_commitment,
)
from medops.hrp_budget.services.reservation_release_control import release_write_active
from medops.services.errors import raise_ione_error

_active: ContextVar[CommitmentConfirmationWrite | None] = ContextVar(
	"medops_commitment_confirmation_write", default=None
)


@dataclass(slots=True)
class CommitmentConfirmationWrite:
	state: LockedCommitmentState = field(repr=False)
	confirmed: CommitmentEvidence = field(repr=False)
	proof: str = field(repr=False)
	values: Mapping[str, object] = field(repr=False)
	document: object = field(default=None, repr=False)
	inserted: bool = False


def enabled():
	if frappe.conf.get("medops_internal_commitment_confirmation_enabled") is not True:
		raise_ione_error("OPERATION_NOT_ALLOWED")


def timestamp():
	return now_datetime().isoformat(sep=" ", timespec="microseconds")


def read_commitment_confirmation(name, *, lock=False):
	doc = frappe.get_doc(DOCTYPE, name, for_update=lock)
	item, confirmed = verify_commitment_evidence_row(
		{field: doc.get(field) for field in COMMITMENT_ROW_FIELDS}
	)
	if confirmed.event_kind != "Confirmation":
		raise ValueError("independent commitment confirmation required")
	return item, confirmed, doc


def _proof(state, confirmed, proof):
	try:
		verify_commitment_proof(
			state.item,
			confirmed,
			proof=proof,
			actor=frappe.session.user,
			session_digest=commitment_session(state.item.reservation),
			now=timestamp(),
		)
	except ValueError as error:
		raise_ione_error("PERMISSION_DENIED", cause=error)


@contextmanager
def expected_commitment_confirmation(state, confirmed, proof):
	"""Allow one exact Confirmation append after full native state revalidation.

	This context cannot authorize a Decision or a financial event. Only the
	caller owning the transaction may commit; any failure requires full rollback.
	"""
	try:
		enabled()
		if _active.get() is not None or initial_write_active() or release_write_active():
			raise_ione_error("OPERATION_NOT_ALLOWED")
		require_commitment_evidence_storage()
		state = recheck_reservation_commitment(state)
		proof = ConfirmationProof(proof)
		_proof(state, confirmed, proof)
		material, _ = material_source_snapshot(asdict(state.inspection.source))
		order, _ = state.purchase.commitment_snapshot(state.inspection.source)
		values = MappingProxyType(
			commitment_evidence_row(state.item, confirmed, material_snapshot=material, order_snapshot=order)
		)
		if confirmed.event_kind != "Confirmation":
			raise_ione_error("OPERATION_NOT_ALLOWED")
		write = CommitmentConfirmationWrite(state, confirmed, proof, values)
		token = _active.set(write)
		try:
			yield write
			if not write.inserted or write.document is None:
				raise_ione_error("OPERATION_NOT_ALLOWED")
			item, persisted, doc = read_commitment_confirmation(confirmed.name, lock=True)
			if item != state.item or persisted != confirmed or doc.owner != state.actor:
				raise_ione_error("CONFLICT")
			check_persisted_document(doc, values, 0)
			_proof(state, confirmed, proof)
			recheck_reservation_commitment(state)
		finally:
			_active.reset(token)
	except (TypeError, ValueError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise


def require_commitment_confirmation_write(doc, *, named=True):
	"""Recheck exact row, document identity, actor/session/proof and native state."""
	try:
		enabled()
		write = _active.get()
		if (
			write is None
			or write.inserted
			or doc.doctype != DOCTYPE
			or not doc.is_new()
			or int(doc.docstatus) != 0
		):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if write.document is None:
			write.document = doc
		elif write.document is not doc:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if doc.owner != write.state.actor or (named and doc.name != write.values["name"]):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		for meta in doc.meta.fields:
			if meta.fieldtype in {"Section Break", "Column Break", "Tab Break"}:
				continue
			if meta.fieldname not in write.values or not same_value(
				meta, doc.get(meta.fieldname), write.values[meta.fieldname]
			):
				raise_ione_error("CONFIGURATION_INVALID")
		_proof(write.state, write.confirmed, write.proof)
		check_commitment_state_access(write.state)
		return write
	except (TypeError, ValueError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise


def prepare_commitment_confirmation(reservation, order, line, *, amount, reason):
	"""Persist a private preview through the real controller, never auto-approve.

	No public endpoint or DomainService wrapper exists yet. Caller owns the clean
	transaction and must provide audit/idempotency before external exposure.
	"""
	try:
		enabled()
		state = inspect_reservation_commitment(reservation, order, line, amount=amount, reason=reason)
		proof = ConfirmationProof(secrets.token_urlsafe(32))
		start = now_datetime()
		confirmed = commitment_confirmation(
			state.item,
			actor=state.actor,
			proof_hash=hashlib.sha256(proof.encode("ascii")).hexdigest(),
			session_digest=state.session,
			recorded_at=start.isoformat(sep=" ", timespec="microseconds"),
			expires_at=(start + timedelta(seconds=CONFIRMATION_SECONDS)).isoformat(
				sep=" ", timespec="microseconds"
			),
		)
		with expected_commitment_confirmation(state, confirmed, proof) as write:
			doc = exact_new_document(DOCTYPE, write.values)
			doc.insert(ignore_permissions=True)
		return {
			"confirmation": confirmed.name,
			"proof": proof,
			"expires_at": confirmed.expires_at,
			"intent_digest": state.item.intent_digest,
			"reservation": state.item.reservation,
			"purchase_order": state.purchase.order,
			"purchase_order_line": state.purchase.line,
			"amount": state.item.amount,
			"currency": state.item.currency,
			"precision": state.item.precision,
		}
	except (TypeError, ValueError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
