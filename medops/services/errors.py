from __future__ import annotations

from collections.abc import Iterable
from typing import NoReturn

import frappe

from medops.common.error_catalog import (
	ErrorCatalogError,
	ErrorDefinition,
	IoneApplicationError,
	load_error_catalog,
	validate_error_translations,
)
from medops.services.audit_context import (
	emit_audit_event,
	ensure_audit_context,
	service_audit_scope,
)

ERROR_CATALOG_ROLES = frozenset({"System Manager", "HRP System Manager"})
INTERNAL_ERROR_FALLBACK = ErrorDefinition(
	key="INTERNAL_ERROR",
	code="IONE-CORE-0012",
	category="internal",
	http_status=500,
	message="An unexpected error occurred.",
	retryable=False,
	log_level="error",
)


def _new_error_id() -> str:
	return frappe.generate_hash(length=16)


def _attach_public_response(error: IoneApplicationError) -> None:
	context = ensure_audit_context()
	response = getattr(frappe.local, "response", None)
	if response is not None:
		response["ione_error"] = {
			**error.as_public_dict(),
			"correlation_id": context.correlation_id,
			"request_id": context.request_id,
		}
	headers = getattr(frappe.local, "response_headers", None)
	if headers is not None:
		headers["X-Ione-Error-Code"] = error.code
		headers["X-Ione-Error-ID"] = error.error_id


def _audit_error(error: IoneApplicationError, cause: BaseException | None) -> None:
	payload: dict[str, object] = {
		"event": "ione_error_raised",
		"error_id": error.error_id,
		"code": error.code,
		"category": error.category,
		"http_status": error.http_status_code,
		"retryable": error.retryable,
	}
	if cause is not None:
		payload["cause_type"] = type(cause).__name__
	emit_audit_event(
		"ione_error_raised",
		level=error.definition.log_level,
		logger_name="medops.errors",
		**{key: value for key, value in payload.items() if key != "event"},
	)


def build_ione_error(
	key_or_code: str,
	*,
	error_id: str | None = None,
) -> IoneApplicationError:
	catalog = load_error_catalog()
	definition = catalog.get(key_or_code)
	return IoneApplicationError(
		definition,
		error_id or _new_error_id(),
		public_message=frappe._(definition.message),
	)


def _build_internal_error_fallback() -> IoneApplicationError:
	return IoneApplicationError(
		INTERNAL_ERROR_FALLBACK,
		_new_error_id(),
		public_message=frappe._(INTERNAL_ERROR_FALLBACK.message),
	)


def raise_ione_error(
	key_or_code: str,
	*,
	cause: BaseException | None = None,
) -> NoReturn:
	"""Raise one controlled error without exposing the source exception or request payload."""
	try:
		error = build_ione_error(key_or_code)
	except ErrorCatalogError as catalog_error:
		error = _build_internal_error_fallback()
		cause = catalog_error
	_attach_public_response(error)
	_audit_error(error, cause)
	frappe.throw(
		error.public_message,
		error,
		title=frappe._("Request failed"),
	)
	raise AssertionError("frappe.throw must raise")


def require_authenticated_user() -> None:
	if frappe.session.user == "Guest":
		raise_ione_error("AUTHENTICATION_REQUIRED")


def require_roles(roles: Iterable[str]) -> None:
	require_authenticated_user()
	if not frozenset(roles).intersection(frappe.get_roles()):
		raise_ione_error("PERMISSION_DENIED")


def get_error_catalog_status() -> dict[str, object]:
	"""Return the stable public contract; error creation remains Git-only."""
	with service_audit_scope():
		require_roles(ERROR_CATALOG_ROLES)
		try:
			catalog = load_error_catalog()
			validate_error_translations(catalog)
		except ErrorCatalogError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		result = catalog.as_public_dict(translate=frappe._)
		result["http_write_enabled"] = False
		emit_audit_event(
			"error_catalog_read",
			logger_name="medops.errors",
			catalog_sha256=catalog.sha256,
			error_count=len(catalog.errors),
		)
		return result
