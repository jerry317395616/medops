from __future__ import annotations

from typing import Any

import frappe

from medops.api.v1.finance.planning import request_guard
from medops.common.budget_ledger import (
	build_release_preview,
	build_release_request,
	build_reverse_request,
	build_revise_release_request,
)
from medops.common.budget_release_directory import build_release_directory
from medops.common.budget_workflow import build_human_confirmation
from medops.common.organization import normalize_reference
from medops.hrp_budget.api.statement_reads import statement_connection
from medops.hrp_budget.services import release, release_confirmation
from medops.hrp_budget.services.approval import require_web_session
from medops.hrp_budget.services.planning_access import MANAGER_ROLES
from medops.hrp_budget.services.release_access import RELEASE_READ_ROLES
from medops.hrp_budget.services.release_directory import (
	ReadReleaseDirectory,
	ReadReleaseRecord,
	ReleaseRecordRequest,
)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def create(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, MANAGER_ROLES, correlation_id):
		require_web_session()
		return release.create_budget_release(build_release_request(payload), correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def create_reversal(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, MANAGER_ROLES, correlation_id):
		require_web_session()
		return release.create_budget_reversal(build_reverse_request(payload), correlation_id=correlation_id)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def revise(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, MANAGER_ROLES, correlation_id):
		require_web_session()
		return release.revise_budget_release(
			build_revise_release_request(payload), correlation_id=correlation_id
		)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def prepare(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, MANAGER_ROLES, correlation_id):
		return release_confirmation.prepare_release_confirmation(
			build_release_preview(payload), correlation_id=correlation_id
		)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def confirm(payload: Any = None, correlation_id: Any = None, **extra):
	with request_guard(extra, MANAGER_ROLES, correlation_id):
		return release_confirmation.confirm_release_authorization(
			build_human_confirmation(payload), correlation_id=correlation_id
		)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_record(name: Any = None, **extra):
	with request_guard(extra, RELEASE_READ_ROLES):
		command = ReleaseRecordRequest(normalize_reference(name, label="name"))
		with statement_connection():
			return ReadReleaseRecord().execute(command).result


@frappe.whitelist(allow_guest=True, methods=["GET"])
def list_records(filters: Any = None, offset: Any = 0, limit: Any = 50, expected_digest: Any = None, **extra):
	with request_guard(extra, RELEASE_READ_ROLES):
		command = build_release_directory(
			"records", filters, offset=offset, limit=limit, expected_digest=expected_digest
		)
		with statement_connection():
			return ReadReleaseDirectory().execute(command).result


@frappe.whitelist(allow_guest=True, methods=["GET"])
def eligible_sources(
	filters: Any = None, offset: Any = 0, limit: Any = 50, expected_digest: Any = None, **extra
):
	with request_guard(extra, RELEASE_READ_ROLES):
		command = build_release_directory(
			"eligible", filters, offset=offset, limit=limit, expected_digest=expected_digest
		)
		with statement_connection():
			return ReadReleaseDirectory().execute(command).result
