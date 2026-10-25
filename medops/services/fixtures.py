from __future__ import annotations

from pathlib import Path

import frappe

from medops.common.constants import APP_NAME
from medops.common.fixture_policy import (
	FixturePolicyError,
	inspect_fixture_repository,
	load_fixture_policy,
)
from medops.services.environment import get_environment_status
from medops.services.errors import raise_ione_error, require_roles


def _fixture_directory() -> Path:
	return Path(frappe.get_app_path(APP_NAME, "fixtures"))


def _require_system_manager() -> None:
	require_roles({"System Manager"})


def get_fixture_governance_status() -> dict[str, object]:
	_require_system_manager()
	try:
		policy = load_fixture_policy()
		repository = inspect_fixture_repository(policy, _fixture_directory())
	except FixturePolicyError as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)
	return {
		"policy": policy.as_public_dict(),
		"repository": repository.as_dict(),
		"export": {
			"http_write_enabled": False,
			"development_environment_required": True,
			"explicit_confirmation_required": True,
		},
	}


def assert_fixture_export_allowed() -> dict[str, object]:
	"""Fail closed before the OS-level fixture exporter writes app source."""
	status = get_environment_status()
	if (
		not status.get("managed")
		or status.get("name") != "development"
		or not frappe.conf.get("developer_mode")
		or not frappe.conf.get("allow_tests")
		or status.get("external_integrations_enabled")
	):
		raise_ione_error("OPERATION_NOT_ALLOWED")
	try:
		policy = load_fixture_policy()
		repository = inspect_fixture_repository(policy, _fixture_directory())
	except FixturePolicyError as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)
	return {
		"status": "ok",
		"environment": "development",
		"schema_version": policy.schema_version,
		"repository_sha256": repository.sha256,
	}
