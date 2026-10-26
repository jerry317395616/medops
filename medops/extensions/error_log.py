from __future__ import annotations

import json
import sys

import frappe
from frappe.core.doctype.error_log.error_log import ErrorLog

from medops.common.diagnostic_privacy import exception_trace, safe_metadata
from medops.services.diagnostic_privacy import install_diagnostic_privacy, is_owned_error


class MedOpsErrorLogMixin(ErrorLog):
	def _protect_medops_error(self):
		try:
			metadata = json.loads(self.metadata or "{}")
		except (TypeError, ValueError):
			metadata = {}
		already_safe = isinstance(metadata, dict) and metadata.get("medops_values_redacted") is True
		if not (already_safe or is_owned_error()):
			return
		install_diagnostic_privacy()
		error = sys.exc_info()[1]
		if error is not None or not already_safe:
			self.error = exception_trace(error)
			self.method = type(error).__name__ if error is not None else "MedOps diagnostic"
			self.reference_name = None
		self.metadata = frappe.as_json(safe_metadata(metadata))
		# Keep native fingerprint, trace_id, timestamps, ownership and persistence.

	def before_insert(self):
		self._protect_medops_error()
		if parent := getattr(super(), "before_insert", None):
			return parent()

	def deferred_insert(self):
		# Native deferred insertion serializes to Redis without validate/insert.
		self._protect_medops_error()
		return super().deferred_insert()
