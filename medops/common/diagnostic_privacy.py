"""Value-free diagnostics for MedOps errors, including late SDK enrichment."""

from __future__ import annotations

import re
import traceback

REDACTED = "[MedOps diagnostic values redacted]"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.<>]*$")
_TRACE_ID = re.compile(r"^[a-fA-F0-9]{16,64}$")
RPC_PREFIXES = ("/api/method/", "/api/v1/method/", "/api/v2/method/")


def owned_method(path: object, command: object = None) -> str | None:
	methods = [command]
	if isinstance(path, str):
		methods.extend(
			path[len(prefix) :].split("/")[0] for prefix in RPC_PREFIXES if path.startswith(prefix)
		)
	return next(
		(
			method
			for method in methods
			if isinstance(method, str) and method.startswith("medops.") and _IDENTIFIER.fullmatch(method)
		),
		None,
	)


def exception_trace(error: BaseException | None) -> str:
	"""Retain frame locations and exception classes, without messages or locals."""
	if error is None:
		return REDACTED

	parts = ["Traceback (MedOps values redacted):"]
	seen = set()
	while error is not None and id(error) not in seen:
		seen.add(id(error))
		for frame, line in traceback.walk_tb(error.__traceback__):
			code = frame.f_code
			parts.append(f'  File "{code.co_filename}", line {line}, in {code.co_name}')
		parts.append(f"{type(error).__module__}.{type(error).__name__}: {REDACTED}")
		error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
	return "\n".join(parts)


def safe_metadata(metadata: object) -> dict:
	metadata = metadata if isinstance(metadata, dict) else {}
	result: dict = {"medops_values_redacted": True}
	if isinstance(metadata.get("type"), str) and metadata["type"] in {"http_request", "background_job"}:
		result["type"] = metadata["type"]
	if isinstance(metadata.get("method"), str) and metadata["method"] in {
		"GET",
		"POST",
		"PUT",
		"DELETE",
		"PATCH",
		"OPTIONS",
		"HEAD",
	}:
		result["method"] = metadata["method"]
	if method := owned_method(
		metadata.get("path"), metadata.get("medops_method") or metadata.get("job_name")
	):
		result["medops_method"] = method
	if isinstance(metadata.get("queue"), str) and metadata["queue"] in {"short", "default", "long"}:
		result["queue"] = metadata["queue"]
	return result


def _identifier(value: object, fallback: str) -> str:
	return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else fallback


def _object(value: object) -> dict:
	return value if isinstance(value, dict) else {}


def _objects(value: object) -> list[dict]:
	return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def safe_event(event: dict) -> dict:
	"""Allow only diagnostic structure; inputs can arrive after event processors."""
	result: dict = {"platform": "python", "message": REDACTED, "tags": {"medops_values_redacted": "true"}}
	if isinstance(event.get("level"), str) and event["level"] in {
		"debug",
		"info",
		"warning",
		"error",
		"fatal",
	}:
		result["level"] = event["level"]
	for field in ("event_id",):
		if isinstance(event.get(field), str) and _TRACE_ID.fullmatch(event[field]):
			result[field] = event[field]
	values = []
	for exception in _objects(_object(event.get("exception")).get("values")):
		frames = []
		for frame in _objects(_object(exception.get("stacktrace")).get("frames")):
			location = {
				"function": _identifier(frame.get("function"), "unknown"),
				"module": _identifier(frame.get("module"), "unknown"),
			}
			filename = frame.get("filename")
			if isinstance(filename, str) and filename.endswith(".py") and "\n" not in filename:
				location["filename"] = filename
			if type(frame.get("lineno")) is int:
				location["lineno"] = frame["lineno"]
			if type(frame.get("in_app")) is bool:
				location["in_app"] = frame["in_app"]
			frames.append(location)
		values.append(
			{
				"type": _identifier(exception.get("type"), "Error"),
				"module": _identifier(exception.get("module"), "unknown"),
				"value": REDACTED,
				"stacktrace": {"frames": frames},
			}
		)
	if values:
		result["exception"] = {"values": values}
	trace = _object(_object(event.get("contexts")).get("trace"))
	trace = {
		key: value
		for key, value in trace.items()
		if key in {"trace_id", "span_id", "parent_span_id"}
		and isinstance(value, str)
		and _TRACE_ID.fullmatch(value)
	}
	if trace:
		result["contexts"] = {"trace": trace}
	return result


class DiagnosticEvent(dict):
	def __sentry_repr__(self) -> dict:
		# The pinned SDK invokes this after *all* scope processors, including
		# Frappe's later WSGI request/header enrichment. Return JSON primitives.
		return safe_event(self)
