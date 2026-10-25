from __future__ import annotations

from typing import cast

import frappe

from medops.common.domain_service import DomainServiceDefinition, DomainServiceExecution, canonical_json
from medops.common.numbering import (
	NumberAllocation,
	NumberingContractError,
	NumberingSchemeUpsert,
	build_number_allocation,
	counter_key_for,
	dimensions_digest_for,
	render_number,
	reset_bucket_for,
	template_digest_for,
)
from medops.hrp_foundation.doctype.hrp_number_reservation.hrp_number_reservation import (
	HRPNumberReservation,
)
from medops.hrp_foundation.doctype.hrp_numbering_scheme.hrp_numbering_scheme import (
	HRPNumberingScheme,
)
from medops.services.audit_context import emit_audit_event, ensure_audit_context
from medops.services.data_access import public_fields_readable, require_scope, require_scoped_read
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_roles

NUMBERING_ADMIN_ROLES = frozenset({"System Manager", "HRP System Manager"})
NUMBERING_ALLOCATION_ROLES = frozenset(
	{"System Manager", "HRP System Manager", "HRP User", "HRP Integration User"}
)
NUMBERING_READ_ROLES = frozenset({*NUMBERING_ALLOCATION_ROLES, "HRP Auditor"})


def _execution_payload(execution: DomainServiceExecution) -> dict[str, object]:
	return {
		**execution.result,
		"correlation_id": execution.correlation_id,
		"request_id": execution.request_id,
		"idempotency_replayed": execution.idempotency_replayed,
	}


def _scheme_doc(name: str) -> HRPNumberingScheme:
	if not frappe.db.exists("HRP Numbering Scheme", name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(HRPNumberingScheme, frappe.get_doc("HRP Numbering Scheme", name))


def _assert_scope(
	*,
	company: str,
	hospital: str,
	organization_unit: str | None,
	valid_from: str | None,
	valid_to: str | None,
	lock: bool,
) -> None:
	hospital_rows = frappe.db.sql(
		"""
		SELECT company, enabled, valid_from, valid_to
		FROM `tabHRP Hospital`
		WHERE name = %s
		"""
		+ (" FOR UPDATE" if lock else ""),
		hospital,
		as_dict=True,
	)
	if not hospital_rows:
		raise_ione_error("RESOURCE_NOT_FOUND")
	hospital_row = hospital_rows[0]
	if hospital_row.company != company:
		raise_ione_error("CONFLICT")
	if not bool(hospital_row.enabled):
		raise_ione_error("INVALID_STATE_TRANSITION")
	if valid_from and hospital_row.valid_from and valid_from < str(hospital_row.valid_from):
		raise_ione_error("CONFLICT")
	if valid_to and hospital_row.valid_to and valid_to > str(hospital_row.valid_to):
		raise_ione_error("CONFLICT")
	if organization_unit is None:
		return

	unit_rows = frappe.db.sql(
		"""
		SELECT
			unit.company,
			unit.hospital,
			unit.enabled,
			unit.valid_from,
			unit.valid_to,
			version.docstatus,
			version.status
		FROM `tabHRP Organization Unit` AS unit
		INNER JOIN `tabHRP Organization Version` AS version
			ON version.name = unit.organization_version
		WHERE unit.name = %s
		"""
		+ (" FOR UPDATE" if lock else ""),
		organization_unit,
		as_dict=True,
	)
	if not unit_rows:
		raise_ione_error("RESOURCE_NOT_FOUND")
	unit = unit_rows[0]
	if unit.company != company or unit.hospital != hospital:
		raise_ione_error("CONFLICT")
	if not bool(unit.enabled) or int(unit.docstatus) != 1 or unit.status != "Published":
		raise_ione_error("INVALID_STATE_TRANSITION")
	if valid_from and unit.valid_from and valid_from < str(unit.valid_from):
		raise_ione_error("CONFLICT")
	if valid_to and unit.valid_to and valid_to > str(unit.valid_to):
		raise_ione_error("CONFLICT")


def _assert_scheme_contract(scheme: HRPNumberingScheme) -> None:
	try:
		digest = template_digest_for(
			template=scheme.template,
			reset_policy=scheme.reset_policy,
			sequence_digits=int(scheme.sequence_digits),
			start_number=int(scheme.start_number),
		)
	except (NumberingContractError, ValueError) as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)
	if digest != scheme.template_digest:
		raise_ione_error("CONFIGURATION_INVALID")


def _operational_scheme(name: str, *, business_date: str) -> HRPNumberingScheme:
	rows = frappe.db.sql(
		"""
		SELECT name
		FROM `tabHRP Numbering Scheme`
		WHERE name = %s
		FOR UPDATE
		""",
		name,
		as_dict=True,
	)
	if not rows:
		raise_ione_error("RESOURCE_NOT_FOUND")
	scheme = _scheme_doc(name)
	_assert_scheme_contract(scheme)
	if not bool(scheme.enabled):
		raise_ione_error("INVALID_STATE_TRANSITION")
	if scheme.valid_from and business_date < str(scheme.valid_from):
		raise_ione_error("INVALID_STATE_TRANSITION")
	if scheme.valid_to and business_date > str(scheme.valid_to):
		raise_ione_error("INVALID_STATE_TRANSITION")
	_assert_scope(
		company=scheme.company,
		hospital=scheme.hospital,
		organization_unit=scheme.organization_unit or None,
		valid_from=business_date,
		valid_to=business_date,
		lock=True,
	)
	return scheme


def _next_counter_value(counter_key: str) -> int:
	frappe.db.sql(
		"""
		INSERT INTO `tabSeries` (`name`, `current`)
		VALUES (%s, 1)
		ON DUPLICATE KEY UPDATE `current` = `current` + 1
		""",
		counter_key,
	)
	rows = frappe.db.sql(
		"""
		SELECT `current`
		FROM `tabSeries`
		WHERE name = %s
		FOR UPDATE
		""",
		counter_key,
		as_dict=True,
	)
	if not rows:
		raise_ione_error("INTERNAL_ERROR")
	try:
		value = int(rows[0].current)
	except (TypeError, ValueError) as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)
	if value < 1:
		raise_ione_error("CONFIGURATION_INVALID")
	return value


class UpsertNumberingSchemeService(DomainService[NumberingSchemeUpsert]):
	definition = DomainServiceDefinition(
		name="hrp_foundation.numbering_scheme.upsert",
		version=1,
		kind="command",
		required_roles=NUMBERING_ADMIN_ROLES,
	)

	def authorize_result(self, result: dict[str, object]) -> None:
		doc = frappe.get_doc("HRP Numbering Scheme", str(result["name"]))
		require_scoped_read(doc)
		if not public_fields_readable(doc):
			raise_ione_error("PERMISSION_DENIED")

	def request_payload(self, command: NumberingSchemeUpsert) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: NumberingSchemeUpsert) -> None:
		require_scope(
			"HRP Numbering Scheme",
			company=command.company,
			hospital=command.hospital,
			organization_unit=command.organization_unit,
			action="write" if command.expected_revision else "create",
		)
		if not frappe.db.exists("Company", command.company):
			raise_ione_error("RESOURCE_NOT_FOUND")
		if command.scheme_name and not frappe.db.exists("HRP Numbering Scheme", command.scheme_name):
			raise_ione_error("RESOURCE_NOT_FOUND")

	def perform(self, command: NumberingSchemeUpsert) -> dict[str, object]:
		if command.expected_revision == 0:
			_assert_scope(
				company=command.company,
				hospital=command.hospital,
				organization_unit=command.organization_unit,
				valid_from=command.valid_from,
				valid_to=command.valid_to,
				lock=True,
			)
			existing_name = frappe.db.exists("HRP Numbering Scheme", command.code)
			if existing_name or command.scheme_name:
				raise_ione_error("CONFLICT")
			doc = cast(
				HRPNumberingScheme,
				frappe.get_doc(
					{
						"doctype": "HRP Numbering Scheme",
						"code": command.code,
						"display_name": command.display_name,
						"company": command.company,
						"hospital": command.hospital,
						"organization_unit": command.organization_unit,
						"template": command.template,
						"reset_policy": command.reset_policy,
						"sequence_digits": command.sequence_digits,
						"start_number": command.start_number,
						"enabled": int(command.enabled),
						"valid_from": command.valid_from,
						"valid_to": command.valid_to,
						"revision": 1,
						"remarks": command.remarks,
					}
				),
			)
			doc.flags.numbering_service_write = True
			try:
				doc.insert(ignore_permissions=True)
			except (frappe.DuplicateEntryError, frappe.UniqueValidationError) as exc:
				raise_ione_error("CONFLICT", cause=exc)
			emit_audit_event(
				"numbering_scheme_created",
				logger_name="medops.numbering",
				scheme_digest=command.template_digest,
				revision=1,
				dimension_count=len(command.dimension_keys),
			)
			return {**doc.as_public_dict(), "changed": True, "changed_fields": ["created"]}

		name = command.scheme_name or command.code
		current_revision = HRPNumberingScheme.lock_revision(command.expected_revision, name)
		_assert_scope(
			company=command.company,
			hospital=command.hospital,
			organization_unit=command.organization_unit,
			valid_from=command.valid_from,
			valid_to=command.valid_to,
			lock=True,
		)
		doc = _scheme_doc(name)
		identity = (doc.code, doc.company, doc.hospital, doc.organization_unit or None)
		requested_identity = (
			command.code,
			command.company,
			command.hospital,
			command.organization_unit,
		)
		if identity != requested_identity:
			raise_ione_error("OPERATION_NOT_ALLOWED")

		fields = (
			"display_name",
			"template",
			"reset_policy",
			"sequence_digits",
			"start_number",
			"enabled",
			"valid_from",
			"valid_to",
			"remarks",
		)
		changed_fields: list[str] = []
		for fieldname in fields:
			current_value = doc.get(fieldname)
			requested_value = getattr(command, fieldname)
			if fieldname == "enabled":
				current_value = bool(current_value)
			elif fieldname in {"sequence_digits", "start_number"}:
				current_value = int(current_value)
			elif fieldname in {"valid_from", "valid_to"}:
				current_value = str(current_value) if current_value else None
			else:
				current_value = str(current_value) if current_value else None
			if current_value != requested_value:
				changed_fields.append(fieldname)
		if not changed_fields:
			return {**doc.as_public_dict(), "changed": False, "changed_fields": []}

		for fieldname in fields:
			value = getattr(command, fieldname)
			doc.set(fieldname, int(value) if fieldname == "enabled" else value)
		doc.flags.numbering_service_write = True
		doc.flags.locked_revision = current_revision
		doc.save(ignore_permissions=True)
		emit_audit_event(
			"numbering_scheme_changed",
			logger_name="medops.numbering",
			before_revision=current_revision,
			after_revision=doc.revision,
			changed_field_count=len(changed_fields),
			changed_fields=",".join(changed_fields),
			scheme_digest=doc.template_digest,
		)
		return {
			**doc.as_public_dict(),
			"changed": True,
			"changed_fields": changed_fields,
		}


class AllocateNumberService(DomainService[NumberAllocation]):
	definition = DomainServiceDefinition(
		name="hrp_foundation.numbering.allocate",
		version=1,
		kind="command",
		required_roles=NUMBERING_ALLOCATION_ROLES,
	)

	def authorize_result(self, result: dict[str, object]) -> None:
		doc = frappe.get_doc("HRP Number Reservation", str(result["name"]))
		require_scoped_read(doc)
		if not public_fields_readable(doc):
			raise_ione_error("PERMISSION_DENIED")

	def request_payload(self, command: NumberAllocation) -> dict[str, object]:
		return command.as_request_payload()

	def validate(self, command: NumberAllocation) -> None:
		scheme = _scheme_doc(command.scheme)
		require_scoped_read(scheme)
		if not public_fields_readable(scheme):
			raise_ione_error("PERMISSION_DENIED")
		require_scope(
			"HRP Number Reservation",
			company=scheme.company,
			hospital=scheme.hospital,
			organization_unit=scheme.organization_unit,
			action="create",
		)

	def perform(self, command: NumberAllocation) -> dict[str, object]:
		scheme = _operational_scheme(command.scheme, business_date=command.business_date)
		try:
			bound_command = build_number_allocation(
				scheme=command.scheme,
				dimensions=command.dimensions,
				business_date=command.business_date,
				required_dimension_keys=scheme.get_dimension_keys(),
			)
			counter_key = counter_key_for(
				scheme=scheme.code,
				reset_policy=scheme.reset_policy,
				business_date=bound_command.business_date,
				dimensions=bound_command.dimensions,
			)
			counter_value = _next_counter_value(counter_key)
			sequence_value = int(scheme.start_number) + counter_value - 1
			number = render_number(
				template=scheme.template,
				business_date=bound_command.business_date,
				dimensions=bound_command.dimensions,
				sequence_value=sequence_value,
				sequence_digits=int(scheme.sequence_digits),
			)
		except NumberingContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)

		if frappe.db.exists("HRP Number Reservation", {"number": number}):
			raise_ione_error("CONFLICT")
		context = ensure_audit_context()
		reservation = cast(
			HRPNumberReservation,
			frappe.get_doc(
				{
					"doctype": "HRP Number Reservation",
					"numbering_scheme": scheme.name,
					"scheme_code": scheme.code,
					"number": number,
					"reservation_token": frappe.generate_hash(length=32),
					"business_date": bound_command.business_date,
					"company": scheme.company,
					"hospital": scheme.hospital,
					"organization_unit": scheme.organization_unit or None,
					"dimensions_json": canonical_json(bound_command.dimensions),
					"dimensions_digest": dimensions_digest_for(bound_command.dimensions),
					"reset_bucket": reset_bucket_for(
						scheme.reset_policy,
						bound_command.business_date,
					),
					"counter_key": counter_key,
					"sequence_value": sequence_value,
					"scheme_revision": scheme.revision,
					"template_digest": scheme.template_digest,
					"allocated_by": frappe.session.user,
					"correlation_id": context.correlation_id,
					"request_id": context.request_id,
				}
			),
		)
		reservation.flags.numbering_service_write = True
		try:
			reservation.insert(ignore_permissions=True)
		except (frappe.DuplicateEntryError, frappe.UniqueValidationError) as exc:
			raise_ione_error("CONFLICT", cause=exc)
		emit_audit_event(
			"number_allocated",
			logger_name="medops.numbering",
			scheme_revision=scheme.revision,
			template_digest=scheme.template_digest,
			dimensions_digest=reservation.dimensions_digest,
			reset_bucket=reservation.reset_bucket,
			sequence_value=sequence_value,
		)
		return reservation.as_public_dict()


def upsert_numbering_scheme(
	command: NumberingSchemeUpsert,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		UpsertNumberingSchemeService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def allocate_number(
	command: NumberAllocation,
	*,
	idempotency_key: object | None,
	correlation_id: object | None = None,
) -> dict[str, object]:
	return _execution_payload(
		AllocateNumberService().execute(
			command,
			idempotency_key=idempotency_key,
			correlation_id=correlation_id,
		)
	)


def get_number_reservation(
	reservation_token: str,
	*,
	correlation_id: object | None = None,
) -> dict[str, object]:
	del correlation_id
	require_roles(NUMBERING_READ_ROLES)
	if not frappe.db.exists("HRP Number Reservation", reservation_token):
		raise_ione_error("RESOURCE_NOT_FOUND")
	reservation = cast(
		HRPNumberReservation,
		frappe.get_doc("HRP Number Reservation", reservation_token),
	)
	if not frappe.has_permission(
		"HRP Number Reservation", ptype="read", doc=reservation
	) or not public_fields_readable(reservation):
		raise_ione_error("PERMISSION_DENIED")
	if not NUMBERING_ADMIN_ROLES.intersection(frappe.get_roles()) and (
		"HRP Auditor" not in frappe.get_roles() and reservation.allocated_by != frappe.session.user
	):
		raise_ione_error("PERMISSION_DENIED")
	emit_audit_event(
		"number_reservation_read",
		logger_name="medops.numbering",
		scheme_revision=reservation.scheme_revision,
		template_digest=reservation.template_digest,
		dimensions_digest=reservation.dimensions_digest,
	)
	return reservation.as_public_dict()


__all__ = [
	"NUMBERING_ADMIN_ROLES",
	"NUMBERING_ALLOCATION_ROLES",
	"NUMBERING_READ_ROLES",
	"AllocateNumberService",
	"UpsertNumberingSchemeService",
	"allocate_number",
	"get_number_reservation",
	"upsert_numbering_scheme",
]
