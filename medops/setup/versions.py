from __future__ import annotations

import json
import subprocess
from importlib import import_module
from pathlib import Path
from typing import Any

from packaging.version import Version

import frappe
from frappe.utils import cint

from medops.common.constants import SUPPORTED_MAJOR
from medops.services.errors import raise_ione_error

UPSTREAM_APPS = ("frappe", "erpnext", "hrms")
LOCK_PATH = Path(__file__).resolve().parents[2] / "resolved_versions.lock.json"


def get_runtime_versions() -> dict[str, str]:
	versions: dict[str, str] = {}
	for app in ("frappe", "erpnext", "hrms", "medops"):
		module = import_module(app)
		versions[app] = str(getattr(module, "__version__", "unknown"))
	return versions


def get_locked_versions() -> dict[str, dict[str, str]]:
	payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
	return {
		app: {
			"repository": str(payload["apps"][app]["repository"]),
			"branch": str(payload["apps"][app]["branch"]),
			"commit": str(payload["apps"][app]["commit"]),
			"version": str(payload["apps"][app]["version"]),
		}
		for app in UPSTREAM_APPS
	}


def _get_app_commit(app: str) -> str | None:
	module = import_module(app)
	module_file = getattr(module, "__file__", None)
	if not module_file:
		return None
	app_root = Path(module_file).resolve().parent.parent
	result = subprocess.run(
		["git", "rev-parse", "HEAD"],
		cwd=app_root,
		text=True,
		capture_output=True,
		check=False,
		timeout=5,
	)
	return result.stdout.strip() if result.returncode == 0 else None


def get_runtime_commits() -> dict[str, str | None]:
	return {app: _get_app_commit(app) for app in UPSTREAM_APPS}


def get_version_status() -> dict[str, Any]:
	locked = get_locked_versions()
	versions = get_runtime_versions()
	commits = get_runtime_commits()
	issues: list[str] = []
	unverifiable: list[str] = []
	for app in UPSTREAM_APPS:
		if versions[app] != locked[app]["version"]:
			issues.append(f"{app} version mismatch: expected {locked[app]['version']}, got {versions[app]}")
		if commits[app] is None:
			unverifiable.append(f"{app} commit is unavailable")
		elif commits[app] != locked[app]["commit"]:
			issues.append(f"{app} commit mismatch: expected {locked[app]['commit']}, got {commits[app]}")
	status = "mismatch" if issues else "unverifiable" if unverifiable else "match"
	return {
		"status": status,
		"lock": locked,
		"runtime": {app: {"version": versions[app], "commit": commits[app]} for app in UPSTREAM_APPS},
		"issues": [*issues, *unverifiable],
	}


def validate_runtime_versions() -> dict[str, str]:
	versions = get_runtime_versions()
	incompatible: list[str] = []
	for app in UPSTREAM_APPS:
		raw = versions[app]
		try:
			major = Version(raw).major
		except Exception:
			incompatible.append(f"{app}={raw} (cannot parse)")
			continue
		if major != SUPPORTED_MAJOR:
			incompatible.append(f"{app}={raw}")
	if incompatible:
		raise_ione_error(
			"CONFIGURATION_INVALID",
			cause=RuntimeError(
				f"Frappe ecosystem major mismatch: required={SUPPORTED_MAJOR}, apps={','.join(incompatible)}"
			),
		)
	if cint(frappe.conf.get("medops_enforce_upstream_lock", 0)):
		status = get_version_status()
		if status["status"] != "match":
			raise_ione_error(
				"CONFIGURATION_INVALID",
				cause=RuntimeError("Upstream commit lock mismatch"),
			)
	return versions
