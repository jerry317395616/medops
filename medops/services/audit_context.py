from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import frappe

from medops.common.audit_context import (
	AUDIT_CONTEXT_SCHEMA_VERSION,
	AuditContext,
	AuditContextError,
	normalize_audit_event,
	normalize_audit_fields,
	normalize_correlation_id,
	normalize_logger_name,
	parse_propagation_payload,
)

AUDIT_CONTEXT_LOCAL_KEY = "medops_audit_context"
AUDIT_CONTEXT_SCOPE_DEPTH_KEY = "medops_audit_context_scope_depth"
AUDIT_CONTEXT_JOB_KWARG = "_ione_audit_context"
AUDIT_HEADER_CORRELATION_ID = "X-Correlation-ID"
AUDIT_HEADER_REQUEST_ID = "X-Request-ID"
AUDIT_LOG_LEVELS = frozenset({"info", "warning", "error"})


def _new_identifier(prefix: str) -> str:
	return f"{prefix}-{frappe.generate_hash(length=20)}"


def _current_context() -> AuditContext | None:
	context = getattr(frappe.local, AUDIT_CONTEXT_LOCAL_KEY, None)
	return context if isinstance(context, AuditContext) else None


def _store_context(context: AuditContext) -> AuditContext:
	setattr(frappe.local, AUDIT_CONTEXT_LOCAL_KEY, context)
	_attach_response_headers(context)
	return context


def _attach_response_headers(context: AuditContext) -> None:
	headers = getattr(frappe.local, "response_headers", None)
	if headers is None:
		return
	headers[AUDIT_HEADER_CORRELATION_ID] = context.correlation_id
	headers[AUDIT_HEADER_REQUEST_ID] = context.request_id


def _request_correlation_candidate(explicit: object | None = None) -> tuple[object | None, str]:
	if explicit not in (None, ""):
		return explicit, "parameter"
	form_dict = getattr(frappe.local, "form_dict", None)
	request = getattr(frappe.local, "request", None)
	path = str(getattr(request, "path", ""))
	command = str(form_dict.get("cmd", "")) if form_dict is not None else ""
	is_ione_method = path.startswith("/api/method/medops.") or command.startswith("medops.")
	if form_dict is not None and is_ione_method:
		parameter = form_dict.get("correlation_id")
		if parameter not in (None, ""):
			return parameter, "parameter"
	if request is not None:
		header = request.headers.get(AUDIT_HEADER_CORRELATION_ID)
		if header not in (None, ""):
			return header, "header"
	return None, "generated"


def _build_context(
	*,
	correlation_id: object | None,
	channel: str,
	origin: str,
	parent_request_id: str | None = None,
) -> AuditContext:
	resolved_correlation_id = (
		normalize_correlation_id(correlation_id) if correlation_id is not None else _new_identifier("corr")
	)
	return AuditContext(
		correlation_id=resolved_correlation_id,
		request_id=_new_identifier("req"),
		parent_request_id=parent_request_id,
		channel=channel,
		origin=origin,
	)


def _install_fallback_context(channel: str) -> AuditContext:
	return _store_context(
		_build_context(
			correlation_id=None,
			channel=channel,
			origin="generated",
		)
	)


def start_http_audit_context() -> AuditContext:
	"""Initialize one immutable context before Frappe authenticates and dispatches a request."""
	current = _current_context()
	if current is not None:
		_attach_response_headers(current)
		return current
	candidate, origin = _request_correlation_candidate()
	try:
		return _store_context(
			_build_context(
				correlation_id=candidate,
				channel="http",
				origin=origin,
			)
		)
	except AuditContextError as exc:
		_install_fallback_context("http")
		from medops.services.errors import raise_ione_error

		raise_ione_error("INVALID_REQUEST", cause=exc)


def finish_http_audit_context(*, response: object = None, request: object = None) -> None:
	"""Ensure successful and failed responses both expose the same safe identifiers."""
	del response, request
	context = _current_context()
	if context is None:
		context = start_http_audit_context()
	_attach_response_headers(context)


def start_job_audit_context(
	*,
	method: str,
	kwargs: dict[str, Any],
	transaction_type: str,
) -> AuditContext:
	"""Restore a propagated parent context without passing the carrier to the target method."""
	del method, transaction_type
	carrier = kwargs.pop(AUDIT_CONTEXT_JOB_KWARG, None)
	if carrier is None:
		return _store_context(
			_build_context(
				correlation_id=None,
				channel="job",
				origin="generated",
			)
		)
	try:
		return _store_context(
			parse_propagation_payload(
				carrier,
				request_id=_new_identifier("req"),
			)
		)
	except AuditContextError as exc:
		context = _install_fallback_context("job")
		emit_audit_event(
			"audit_context_carrier_rejected",
			level="warning",
			logger_name="medops.audit",
			cause_type=type(exc).__name__,
		)
		return context


def finish_job_audit_context(
	*,
	method: str,
	kwargs: dict[str, Any],
	result: object = None,
) -> None:
	del method, kwargs, result
	clear_audit_context()


def clear_audit_context() -> None:
	if hasattr(frappe.local, AUDIT_CONTEXT_LOCAL_KEY):
		delattr(frappe.local, AUDIT_CONTEXT_LOCAL_KEY)


def _service_scope_depth() -> int:
	depth = getattr(frappe.local, AUDIT_CONTEXT_SCOPE_DEPTH_KEY, 0)
	return depth if isinstance(depth, int) and depth > 0 else 0


def _clear_service_scope_depth() -> None:
	if hasattr(frappe.local, AUDIT_CONTEXT_SCOPE_DEPTH_KEY):
		delattr(frappe.local, AUDIT_CONTEXT_SCOPE_DEPTH_KEY)


@contextmanager
def service_audit_scope(correlation_id: object | None = None) -> Iterator[AuditContext]:
	"""Bound one direct service invocation without replacing HTTP, job, or nested context."""
	current = _current_context()
	depth = _service_scope_depth()
	if current is not None and (current.channel in {"http", "job"} or depth > 0):
		yield ensure_audit_context(correlation_id)
		return

	clear_audit_context()
	setattr(frappe.local, AUDIT_CONTEXT_SCOPE_DEPTH_KEY, 1)
	try:
		try:
			context = _store_context(
				_build_context(
					correlation_id=correlation_id,
					channel="service",
					origin="parameter" if correlation_id is not None else "generated",
				)
			)
		except AuditContextError as exc:
			_install_fallback_context("service")
			from medops.services.errors import raise_ione_error

			raise_ione_error("INVALID_REQUEST", cause=exc)
		yield context
	finally:
		clear_audit_context()
		_clear_service_scope_depth()


def ensure_audit_context(correlation_id: object | None = None) -> AuditContext:
	"""Return the execution context and reject attempts to replace it mid-execution."""
	current = _current_context()
	if current is None:
		request = getattr(frappe.local, "request", None)
		job = getattr(frappe.local, "job", None)
		if request is not None:
			candidate, origin = _request_correlation_candidate(correlation_id)
			try:
				return _store_context(
					_build_context(
						correlation_id=candidate,
						channel="http",
						origin=origin,
					)
				)
			except AuditContextError as exc:
				_install_fallback_context("http")
				from medops.services.errors import raise_ione_error

				raise_ione_error("INVALID_REQUEST", cause=exc)
		channel = "job" if job is not None else "service"
		try:
			return _store_context(
				_build_context(
					correlation_id=correlation_id,
					channel=channel,
					origin="parameter" if correlation_id is not None else "generated",
				)
			)
		except AuditContextError as exc:
			_install_fallback_context(channel)
			from medops.services.errors import raise_ione_error

			raise_ione_error("INVALID_REQUEST", cause=exc)

	if correlation_id not in (None, ""):
		try:
			normalized = normalize_correlation_id(correlation_id)
		except AuditContextError as exc:
			from medops.services.errors import raise_ione_error

			raise_ione_error("INVALID_REQUEST", cause=exc)
		if normalized != current.correlation_id:
			from medops.services.errors import raise_ione_error

			raise_ione_error("INVALID_REQUEST")
	_attach_response_headers(current)
	return current


def get_audit_context_status() -> dict[str, object]:
	from medops.services.errors import require_authenticated_user

	with service_audit_scope() as context:
		require_authenticated_user()
		emit_audit_event("audit_context_read", logger_name="medops.audit")
		return {
			**context.as_public_dict(),
			"http_write_enabled": False,
		}


def emit_audit_event(
	event: str,
	*,
	level: str = "info",
	logger_name: str = "medops.audit",
	**fields: Any,
) -> dict[str, object]:
	"""Write a context-rich, scalar-only event after rejecting sensitive field names."""
	if level not in AUDIT_LOG_LEVELS:
		raise AuditContextError("audit level is invalid")
	context = ensure_audit_context()
	payload = {
		**context.as_audit_dict(),
		"event": normalize_audit_event(event),
		**normalize_audit_fields(fields),
	}
	logger = frappe.logger(normalize_logger_name(logger_name), allow_site=True)
	getattr(logger, level)(payload)
	return payload


def enqueue_with_audit(
	method: str | Callable[..., Any],
	**kwargs: Any,
) -> Any:
	"""Enqueue a Frappe job with a carrier that the before_job hook consumes."""
	if AUDIT_CONTEXT_JOB_KWARG in kwargs:
		from medops.services.errors import raise_ione_error

		raise_ione_error("INVALID_REQUEST")
	context = ensure_audit_context()
	return frappe.enqueue(
		method,
		**kwargs,
		**{AUDIT_CONTEXT_JOB_KWARG: context.as_propagation_dict()},
	)


__all__ = [
	"AUDIT_CONTEXT_JOB_KWARG",
	"AUDIT_CONTEXT_SCHEMA_VERSION",
	"clear_audit_context",
	"emit_audit_event",
	"enqueue_with_audit",
	"ensure_audit_context",
	"finish_http_audit_context",
	"finish_job_audit_context",
	"get_audit_context_status",
	"service_audit_scope",
	"start_http_audit_context",
	"start_job_audit_context",
]
