from __future__ import annotations

from pathlib import Path

import frappe

from medops.common.change_governance import (
	ChangeGovernanceError,
	inspect_change_governance,
)
from medops.common.constants import APP_NAME
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.errors import raise_ione_error, require_roles

GOVERNANCE_ROLES = frozenset({"System Manager", "HRP System Manager"})


def _repository_root() -> Path:
	return Path(frappe.get_app_path(APP_NAME)).resolve().parent


def _require_governance_reader() -> None:
	require_roles(GOVERNANCE_ROLES)


def get_change_governance_status(correlation_id: str | None = None) -> dict[str, object]:
	"""Return a redacted, read-only view of the Git-governed engineering baseline."""
	with service_audit_scope(correlation_id) as context:
		_require_governance_reader()
		try:
			report = inspect_change_governance(_repository_root())
		except ChangeGovernanceError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		result = report.as_public_dict()
		result["correlation_id"] = context.correlation_id
		result["write_channel"] = "Git pull request only"
		result["http_write_enabled"] = False
		emit_audit_event(
			"change_governance_status_read",
			logger_name="medops.change_governance",
			governance_sha256=report.sha256,
			decision_count=len(report.decisions),
			change_record_count=len(report.changes),
		)
		return result
