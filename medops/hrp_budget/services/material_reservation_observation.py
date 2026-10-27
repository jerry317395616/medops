"""Inspect physical purchase reservation evidence under an existing budget guard."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, localcontext

import frappe

from medops.common.budget_control import BudgetControlKey, ordered_budget_keys
from medops.common.budget_reservation_anchor import ANCHOR_ROW_FIELDS
from medops.common.budget_reservation_events import MAX_RESERVATION_EVENTS, ReservationPosition
from medops.common.budget_reservation_storage import ROW_FIELDS
from medops.common.organization import normalize_reference
from medops.hrp_budget.services.control_guards import assert_budget_guards, invalidate_budget_guards
from medops.hrp_budget.services.material_request_source import (
	MaterialRequestSource,
	locked_material_request_source,
)
from medops.hrp_budget.services.material_reservation_anchor import verify_material_reservation_evidence
from medops.hrp_budget.services.release_execution import refresh_financial_authority
from medops.hrp_master_data.services.budget_dimensions import native_read
from medops.services.data_access import require_scope, scope_allowed
from medops.services.errors import raise_ione_error

RESERVATION = "HRP Budget Reservation"
EVENT = "HRP Budget Reservation Event"
MAX_SOURCE_RESERVATIONS = 100


def _columns(fields: frozenset[str]) -> str:
	# Only module constants are passed here; identifiers never come from a user.
	return ", ".join(f"`{field}`" for field in sorted(fields))


def _source_reservations(source: MaterialRequestSource):
	return frappe.db.sql(
		f"""SELECT {_columns(ANCHOR_ROW_FIELDS)} FROM `tabHRP Budget Reservation`
		WHERE `source_doctype`=%s AND `source_name`=%s AND `source_line`=%s
		ORDER BY `name` LIMIT %s FOR UPDATE""",  # nosec B608 - fixed field and table identifiers
		("Material Request", source.request, source.line, MAX_SOURCE_RESERVATIONS + 1),
		as_dict=True,
	)


def _related_events(source: MaterialRequestSource, names: tuple[str, ...]):
	parameters: tuple[object, ...] = ("Material Request", source.request, source.line)
	parent_clause = ""
	if names:
		parent_clause = " OR `reservation` IN (" + ", ".join(["%s"] * len(names)) + ")"
		parameters += names
	parameters += (MAX_RESERVATION_EVENTS + 1,)
	return frappe.db.sql(
		f"""SELECT {_columns(ROW_FIELDS)} FROM `tabHRP Budget Reservation Event`
		WHERE (`source_doctype`=%s AND `source_name`=%s AND `source_line`=%s){parent_clause}
		ORDER BY `name` LIMIT %s FOR UPDATE""",  # nosec B608 - fixed identifiers and bound placeholders
		parameters,
		as_dict=True,
	)


def _check_permissions(source: MaterialRequestSource, actor: str) -> None:
	if frappe.session.user != actor:
		raise_ione_error("CONFLICT")
	refresh_financial_authority(actor)
	for doctype, fields in ((RESERVATION, ANCHOR_ROW_FIELDS), (EVENT, ROW_FIELDS)):
		frappe.get_meta(doctype, cached=False)
		require_scope(
			doctype,
			company=source.company,
			hospital=source.hospital,
			organization_unit=source.organization_unit,
		)
		probe = frappe.get_doc(
			dict(
				doctype=doctype,
				company=source.company,
				hospital=source.hospital,
				organization_unit=source.organization_unit,
			)
		)
		if not native_read(probe, set(fields), user=actor):
			raise_ione_error("PERMISSION_DENIED")


def _check_row(doctype: str, row: Mapping, fields: frozenset[str], actor: str) -> None:
	doc = frappe.get_doc(dict(row, doctype=doctype))
	if not scope_allowed(doc, user=actor) or not native_read(doc, set(fields), user=actor):
		raise_ione_error("PERMISSION_DENIED")


def guarded_material_reservation_observation(
	source: MaterialRequestSource,
	keys: object,
	*,
	allocations: object,
) -> tuple[ReservationPosition, ...]:
	"""Inspect bounded rows for a previously locked purchase line.

	The caller must lock the native source before acquiring exactly these budget
	guards and derive each allocation and key from an approved policy. Only events
	whose source is this exact current Material Request snapshot are supported;
	other source types or revisions fail closed until adapters exist. The result
	is a structural observation, not proof of authorized writes, complete history,
	availability, or spending authority. Existing writers still bypass the guard,
	and absent-row phantoms, hospital allocation policy and global lock order are
	unproven. The caller must roll back the entire transaction after any failure.
	"""
	actor = frappe.session.user
	try:
		if type(source) is not MaterialRequestSource or not isinstance(allocations, Mapping):
			raise ValueError("locked source and allocation mapping required")
		if len(allocations) > MAX_SOURCE_RESERVATIONS:
			raise ValueError("too many source allocations")
		ordered = ordered_budget_keys(keys)
		allowed = set(ordered)
		for allocation, key in allocations.items():
			if (
				normalize_reference(allocation, label="allocation") != allocation
				or type(key) is not BudgetControlKey
				or key not in allowed
			):
				raise ValueError("canonical allocation and guarded budget key required")
		for key in ordered:
			if (key.company, key.hospital, key.organization_unit, key.project) != (
				source.company,
				source.hospital,
				source.organization_unit,
				source.project,
			):
				raise ValueError("budget keys differ from source scope")
		assert_budget_guards(ordered)
		_check_permissions(source, actor)
		if locked_material_request_source(source.request, source.line, source.organization_unit) != source:
			raise_ione_error("CONFLICT")
		rows = _source_reservations(source)
		if len(rows) > MAX_SOURCE_RESERVATIONS:
			raise ValueError("reservation scope exceeds limit")
		names = tuple(sorted(row.name for row in rows))
		if len(names) != len(set(names)):
			raise ValueError("duplicate reservation names")
		events = _related_events(source, names)
		if len(events) > MAX_RESERVATION_EVENTS:
			raise ValueError("reservation event scope exceeds limit")
		grouped = {name: [] for name in names}
		seen = set()
		for row in events:
			if (
				row.name in seen
				or row.reservation not in grouped
				or (row.source_doctype, row.source_name, row.source_line, row.source_revision)
				!= ("Material Request", source.request, source.line, source.digest())
			):
				raise ValueError("unrecognized or orphan reservation event")
			seen.add(row.name)
			_check_row(EVENT, row, ROW_FIELDS, actor)
			grouped[row.reservation].append(row)
		positions = []
		used_allocations = set()
		with localcontext() as context:
			context.prec = 48
			exposure = Decimal(0)
			for row in rows:
				allocation = row.source_allocation
				if allocation in used_allocations or allocation not in allocations:
					raise ValueError("unknown or duplicate allocation")
				used_allocations.add(allocation)
				_check_row(RESERVATION, row, ANCHOR_ROW_FIELDS, actor)
				position = verify_material_reservation_evidence(
					row,
					sorted(grouped[row.name], key=lambda item: item.sequence),
					source=source,
					allocation=allocation,
					expected_key=allocations[allocation],
				)
				positions.append(position)
				exposure += (
					Decimal(position.open_reservation)
					+ Decimal(position.open_commitment)
					+ Decimal(position.actual)
				)
			if exposure > Decimal(source.amount):
				raise ValueError("combined allocations exceed purchase source")
		_check_permissions(source, actor)
		if locked_material_request_source(source.request, source.line, source.organization_unit) != source:
			raise_ione_error("CONFLICT")
		for row in rows:
			_check_row(RESERVATION, row, ANCHOR_ROW_FIELDS, actor)
		for row in events:
			_check_row(EVENT, row, ROW_FIELDS, actor)
		assert_budget_guards(ordered)
		return tuple(sorted(positions, key=lambda item: item.reservation))
	except ValueError as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
