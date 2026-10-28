"""Scoped recovery of existing decisions without reading or reissuing old proofs."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

import frappe
from frappe.utils import now_datetime

from medops.common.budget_ledger import LEDGER
from medops.common.domain_service import DomainServiceDefinition, fingerprint_json, normalize_sha256
from medops.common.material_source_snapshot import material_source_snapshot, parse_material_source_snapshot
from medops.common.reservation_authorization import (
	AUTHORIZATION_ROW_FIELDS,
	verify_reservation_authorization_rows,
)
from medops.common.reservation_recovery import (
	MAX_RECOVERY_RECORDS,
	ReservationRecoveryQuery,
	build_reservation_recovery,
)
from medops.hrp_budget.services.approval import require_web_session, session_digest
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.material_budget_guards import assert_material_budget_guards
from medops.hrp_budget.services.material_request_source import locked_material_request_source
from medops.hrp_budget.services.material_usage_preview import inspect_purchase_usage
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.reservation_confirmation import require_reservation_actor_scope
from medops.hrp_budget.services.reservation_release_state import read_guarded_reservation_release
from medops.hrp_budget.services.reservation_renewal_lineage import locked_renewal_lineage
from medops.services.data_access import require_scope
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_roles

AUTHORIZATION = "HRP Budget Reservation Authorization"
ROOT = "HRP Budget Reservation"


def _access(command, source):
	require_roles(MANAGER_ROLES)
	require_web_session()
	key = command.key
	if (key.company, key.hospital, key.organization_unit) != (
		source.company,
		source.hospital,
		source.organization_unit,
	):
		raise ValueError("recovery scope differs from checked source")
	for doctype, action in ((ROOT, "write"), (LEDGER, "read")):
		require_scope(
			doctype,
			company=key.company,
			hospital=key.hospital,
			organization_unit=key.organization_unit,
			action=action,
		)


def _names(source):
	# Source/native-field and budget scope have already been checked by the
	# complete inspector. Do not filter untrusted stored digests before verifying
	# each original pair; that would silently hide a malformed pending decision.
	rows = frappe.get_all(
		AUTHORIZATION,
		filters={
			"company": source.company,
			"hospital": source.hospital,
			"organization_unit": source.organization_unit,
			"source_name": source.request,
			"source_line": source.line,
			"event_kind": "Decision",
		},
		pluck="name",
		order_by="name",
		limit_page_length=MAX_RECOVERY_RECORDS + 1,
	)
	if len(rows) > MAX_RECOVERY_RECORDS or len(rows) != len(set(rows)):
		raise ValueError("recovery original decision set exceeds bound")
	return tuple(rows)


def _evidence(name):
	name = normalize_sha256(name, label="authorization")
	try:
		doc = frappe.get_doc(AUTHORIZATION, name, for_update=True)
	except frappe.DoesNotExistError as error:
		raise ValueError("complete immutable authorization pair required") from error
	if int(doc.docstatus) != 0:
		raise ValueError("unsubmitted immutable authorization evidence required")
	return {field: doc.get(field) for field in AUTHORIZATION_ROW_FIELDS}


def _rows(command, inspection):
	source = inspection.source
	actor = frappe.session.user
	sid = frappe.session.sid
	_access(command, source)
	names = _names(source)
	rows, tokens, seen = [], [], set()
	stamp = now_datetime()
	for name in names:
		second = _evidence(name)
		first = _evidence(second["confirmation"])
		item, original = verify_reservation_authorization_rows(first, second)
		facts = parse_material_source_snapshot(
			item.source_snapshot, expected_digest=item.source_snapshot_digest
		)
		if (
			facts["request"],
			facts["line"],
			item.key.company,
			item.key.hospital,
			item.key.organization_unit,
		) != (
			source.request,
			source.line,
			source.company,
			source.hospital,
			source.organization_unit,
		) or item.reservation_identity in seen:
			raise ValueError("recovery evidence source, scope or uniqueness differs")
		seen.add(item.reservation_identity)
		if item.key != command.key:
			continue
		require_reservation_actor_scope(
			item, actor, session_digest("budget-reservation:" + item.reservation_identity)
		)
		roots = frappe.get_all(
			ROOT,
			filters={"source_identity_key": item.reservation_identity},
			pluck="name",
			limit_page_length=2,
		)
		if len(roots) > 1:
			raise ValueError("one immutable root per allocation required")
		row = {
			"confirmation": first["name"],
			"decision": original.name,
			"intent_digest": item.intent_digest,
			"allocation": item.allocation,
			"amount": item.amount,
			"currency": item.currency,
			"precision": item.precision,
			"budget_date": item.budget_date,
			"recorded_at": original.recorded_at,
			"reservation": None,
			"expires_at": None,
			"renewal_decision": None,
			"intent": None,
		}
		sequence = None
		if roots:
			positions = [value for value in inspection.source_usage if value.reservation == roots[0]]
			if len(positions) != 1:
				raise ValueError("recovery root was not fully inspected")
			state = read_guarded_reservation_release(roots[0], item, source, positions[0])
			row.update(state="Posted", reservation=state.position.reservation)
			sequence = state.position.last_sequence
		elif (item.source_snapshot, item.source_snapshot_digest) != material_source_snapshot(asdict(source)):
			# A changed native source never grants permission to renew an old intent.
			row["state"] = "Source Changed"
		else:
			lineage = locked_renewal_lineage(item, source, first["name"], second["name"])
			latest = lineage.renewals[-1][1] if lineage.renewals else lineage.original
			row.update(
				state="Expired" if stamp >= datetime.fromisoformat(latest.expires_at) else "Decision Valid",
				expires_at=latest.expires_at,
				renewal_decision=latest.name if lineage.renewals else None,
				intent={
					"allocation": item.allocation,
					"currency": item.currency,
					"precision": item.precision,
					"amount": item.amount,
					"budget_date": item.budget_date,
					"reason": item.reason,
				},
			)
		rows.append(row)
		tokens.append(
			{
				key: row[key]
				for key in (
					"decision",
					"intent_digest",
					"state",
					"expires_at",
					"renewal_decision",
					"reservation",
				)
			}
			| {"sequence": sequence}
		)
	if _names(source) != names:
		raise ValueError("recovery directory changed during read")
	if locked_material_request_source(source.request, source.line, source.organization_unit) != source:
		raise ValueError("recovery native source changed")
	_access(command, source)
	if (frappe.session.user, frappe.session.sid) != (actor, sid):
		raise ValueError("recovery request principal changed")
	assert_material_budget_guards(source, [command.key])
	digest = fingerprint_json(
		{"source": command.source_snapshot_digest, "budget": command.key.identity, "rows": tokens}
	)
	if command.expected_digest is not None and command.expected_digest != digest:
		raise ValueError("recovery directory changed between pages")
	rows.sort(key=lambda row: (row["recorded_at"], row["decision"]), reverse=True)
	if command.decision is not None:
		rows = [row for row in rows if row["decision"] == command.decision]
	return {
		"source_snapshot_digest": command.source_snapshot_digest,
		"budget_key": asdict(command.key),
		"rows": rows[command.offset : command.offset + command.limit],
		"total_rows": len(rows),
		"offset": command.offset,
		"limit": command.limit,
		"directory_digest": digest,
		"effect": "read_only",
	}


class ReadReservationRecovery(DomainService[ReservationRecoveryQuery]):
	definition = DomainServiceDefinition(
		name="hrp_budget.reservation.recovery", version=1, kind="query", required_roles=MANAGER_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		require_web_session()

	def validate(self, command):
		if (
			type(command) is not ReservationRecoveryQuery
			or build_reservation_recovery(
				{
					"source_snapshot": command.source_snapshot,
					"source_snapshot_digest": command.source_snapshot_digest,
					"budget_key": asdict(command.key),
					"offset": command.offset,
					"limit": command.limit,
					"expected_digest": command.expected_digest,
					"decision": command.decision,
				}
			)
			!= command
		):
			raise_ione_error("INVALID_REQUEST")
		self.command = command
		self.inspection = None
		self.principal = (frappe.session.user, frappe.session.sid)

	def request_payload(self, command):
		return {
			"source_snapshot_digest": command.source_snapshot_digest,
			"budget_key_digest": command.key.identity,
			"offset": command.offset,
			"limit": command.limit,
			"expected_digest": command.expected_digest,
			"decision": command.decision,
		}

	def perform(self, command):
		try:
			if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is not None:
				raise_ione_error("OPERATION_NOT_ALLOWED")
			facts = parse_material_source_snapshot(
				command.source_snapshot, expected_digest=command.source_snapshot_digest
			)
			self.inspection = inspect_purchase_usage(
				facts["request"],
				facts["line"],
				command.key.organization_unit,
				expected_source_digest=command.source_snapshot_digest,
				budget_date=command.key.period_start,
				proposed_keys=[asdict(command.key)],
			)
			return _rows(command, self.inspection)
		except (ValueError, TypeError, KeyError) as error:
			invalidate_budget_guards()
			raise_ione_error("CONFLICT", cause=error)
		except BaseException:
			invalidate_budget_guards()
			raise

	def authorize_result(self, result):
		try:
			if (
				self.principal != (frappe.session.user, frappe.session.sid)
				or self.inspection is None
				or _rows(self.command, self.inspection) != result
			):
				raise ValueError("complete current recovery result required")
		except (ValueError, TypeError, KeyError) as error:
			invalidate_budget_guards()
			raise_ione_error("CONFLICT", cause=error)
		except BaseException:
			invalidate_budget_guards()
			raise
