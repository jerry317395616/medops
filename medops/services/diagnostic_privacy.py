from __future__ import annotations

import logging
import sys
from threading import Lock

import frappe

from medops.common.diagnostic_privacy import DiagnosticEvent, exception_trace, owned_method

_install_lock = Lock()
_sdk_installed = False


def is_owned_error() -> bool:
	request = getattr(frappe.local, "request", None)
	form = getattr(frappe.local, "form_dict", None) or {}
	if owned_method(getattr(request, "path", None), form.get("cmd")):
		return True
	job = getattr(frappe.local, "job", None) or {}
	if owned_method(None, job.get("method")):
		return True
	error = sys.exc_info()[1]
	seen = set()
	while error is not None and id(error) not in seen:
		seen.add(id(error))
		tb = error.__traceback__
		while tb is not None:
			if str(tb.tb_frame.f_globals.get("__name__", "")).startswith("medops."):
				return True
			tb = tb.tb_next
		error = error.__cause__
	return False


def protect_event(event, hint):
	del hint
	return DiagnosticEvent(event) if is_owned_error() else event


class MedOpsDiagnosticFilter(logging.Filter):
	def filter(self, record):
		if record.levelno >= logging.ERROR and is_owned_error():
			# Handler filters run after native SiteContextFilter, which appends
			# form_dict. Preserve severity, logger identity and frame locations.
			error = record.exc_info[1] if record.exc_info else sys.exc_info()[1]
			record.msg = exception_trace(error)
			record.args = ()
			record.exc_info = None
			record.exc_text = None
			record.stack_info = None
		return True


def install_diagnostic_privacy() -> None:
	"""Register standard SDK/logging extensions; retain native opt-in and sinks."""
	global _sdk_installed
	with _install_lock:
		# Ensure the native fallback logger already has the filter if SDK
		# capture itself fails before log_error_snapshot creates that logger.
		frappe.logger()
		if not _sdk_installed:
			from sentry_sdk import get_global_scope

			get_global_scope().add_event_processor(protect_event)
			_sdk_installed = True
		for logger in tuple(frappe.loggers.values()):
			if not logger.name.startswith(("None-", "frappe-", "frappe.", "database-")):
				continue
			for handler in tuple(logger.handlers):
				if not any(isinstance(item, MedOpsDiagnosticFilter) for item in handler.filters):
					handler.addFilter(MedOpsDiagnosticFilter())


def protect_medops_diagnostics() -> None:
	if is_owned_error():
		install_diagnostic_privacy()
