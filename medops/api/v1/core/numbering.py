from __future__ import annotations

import frappe

from medops.common.numbering import (
	NumberingContractError,
	build_number_allocation,
	build_numbering_scheme_upsert,
)
from medops.hrp_foundation.services.numbering import (
	allocate_number,
	get_number_reservation,
	upsert_numbering_scheme,
)
from medops.services.audit_context import service_audit_scope
from medops.services.errors import raise_ione_error, require_authenticated_user


@frappe.whitelist(allow_guest=True, methods=["POST"])
def upsert_scheme(
	code: str,
	display_name: str,
	company: str,
	hospital: str,
	template: str,
	reset_policy: str,
	sequence_digits: int | str,
	start_number: int | str = 1,
	enabled: bool | int | str = True,
	organization_unit: str | None = None,
	valid_from: str | None = None,
	valid_to: str | None = None,
	expected_revision: int | str = 0,
	scheme_name: str | None = None,
	remarks: str | None = None,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		try:
			command = build_numbering_scheme_upsert(
				scheme_name=scheme_name,
				code=code,
				display_name=display_name,
				company=company,
				hospital=hospital,
				organization_unit=organization_unit,
				template=template,
				reset_policy=reset_policy,
				sequence_digits=sequence_digits,
				start_number=start_number,
				enabled=enabled,
				valid_from=valid_from,
				valid_to=valid_to,
				expected_revision=expected_revision,
				remarks=remarks,
			)
		except NumberingContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return upsert_numbering_scheme(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def next(
	scheme: str,
	dimensions: str | dict[str, object],
	date: str,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		try:
			command = build_number_allocation(
				scheme=scheme,
				dimensions=dimensions,
				business_date=date,
			)
		except NumberingContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		return allocate_number(
			command,
			idempotency_key=None,
			correlation_id=correlation_id,
		)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_reservation(
	reservation_token: str,
	correlation_id: str | None = None,
) -> dict[str, object]:
	with service_audit_scope(correlation_id):
		require_authenticated_user()
		return get_number_reservation(
			reservation_token,
			correlation_id=correlation_id,
		)


__all__ = ["get_reservation", "next", "upsert_scheme"]
