from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
	sys.path.insert(0, str(REPOSITORY_ROOT))

from medops.common.audit_context import AuditContextError, normalize_correlation_id
from medops.common.constants import APP_NAME
from medops.common.fixture_policy import (
	FixturePolicy,
	FixturePolicyError,
	canonicalize_fixture_repository,
	get_frappe_fixture_hooks,
	inspect_fixture_repository,
	load_fixture_policy,
)

SITE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]*\.localhost$")
PRODUCTION_MARKERS = ("manager.myyr.top", "/prod/", "\\prod\\", "production")


class FixtureManagerError(RuntimeError):
	"""Raised when a fixture source operation is unsafe or invalid."""


@dataclass(frozen=True)
class ExportTarget:
	bench_dir: Path
	site_name: str
	app_dir: Path

	@property
	def site_config_path(self) -> Path:
		return self.bench_dir / "sites" / self.site_name / "site_config.json"

	@property
	def fixture_directory(self) -> Path:
		return self.app_dir / APP_NAME / "fixtures"

	@property
	def audit_path(self) -> Path:
		return self.bench_dir / "logs" / "fixture-export-audit.jsonl"


class CommandRunner:
	def run(self, command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
		result = subprocess.run(
			command,
			cwd=cwd,
			capture_output=True,
			text=True,
			check=False,
		)
		if result.returncode:
			raise FixtureManagerError(f"Command failed: {command[0]}")
		return result


def _contains_production_marker(value: str) -> bool:
	normalized = value.lower().replace("\\", "/")
	return any(marker.replace("\\", "/") in normalized for marker in PRODUCTION_MARKERS)


def build_export_target(
	bench_dir: Path,
	site_name: str,
	*,
	repository_root: Path = REPOSITORY_ROOT,
) -> ExportTarget:
	resolved_bench = bench_dir.expanduser().resolve()
	if not resolved_bench.is_dir() or _contains_production_marker(str(resolved_bench)):
		raise FixtureManagerError("Bench target is missing or production-like")
	if not SITE_NAME_PATTERN.fullmatch(site_name) or _contains_production_marker(site_name):
		raise FixtureManagerError("Fixture export requires a non-production .localhost site")
	app_dir = (resolved_bench / "apps" / APP_NAME).resolve()
	if app_dir != repository_root.resolve():
		raise FixtureManagerError("Run the fixture manager from the Bench medops source checkout")
	target = ExportTarget(bench_dir=resolved_bench, site_name=site_name, app_dir=app_dir)
	if not target.site_config_path.is_file():
		raise FixtureManagerError("Site configuration is missing")
	return target


def _load_site_config(target: ExportTarget) -> dict[str, Any]:
	try:
		payload = json.loads(target.site_config_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise FixtureManagerError("Cannot load site configuration") from exc
	if not isinstance(payload, dict):
		raise FixtureManagerError("Site configuration must be an object")
	return payload


def validate_development_target(target: ExportTarget) -> None:
	config = _load_site_config(target)
	if (
		config.get("medops_environment") != "development"
		or config.get("developer_mode") not in (1, True)
		or config.get("allow_tests") not in (1, True)
		or config.get("medops_external_integrations_enabled") not in (0, False)
	):
		raise FixtureManagerError("Fixture export target is not the managed development site")


def validate_hook_contract(policy: FixturePolicy) -> None:
	import medops.hooks as hooks

	if getattr(hooks, "fixture_auto_order", None) is not policy.fixture_auto_order:
		raise FixtureManagerError("hooks.py fixture_auto_order does not match policy")
	if getattr(hooks, "fixtures", None) != get_frappe_fixture_hooks():
		raise FixtureManagerError("hooks.py fixtures do not match policy")


def validate_repository(
	*,
	policy: FixturePolicy | None = None,
	fixture_directory: Path | None = None,
) -> dict[str, object]:
	active_policy = policy or load_fixture_policy()
	validate_hook_contract(active_policy)
	report = inspect_fixture_repository(
		active_policy,
		fixture_directory or REPOSITORY_ROOT / APP_NAME / "fixtures",
	)
	return {
		"status": "ok",
		"policy": active_policy.as_public_dict(),
		"repository": report.as_dict(),
	}


def build_plan(policy: FixturePolicy) -> dict[str, object]:
	return {
		"status": "ok",
		"operation": "export",
		"app": policy.app,
		"schema_version": policy.schema_version,
		"fixture_auto_order": policy.fixture_auto_order,
		"rules": [rule.as_public_dict() for rule in policy.rules],
		"source_write_requires_confirmation": True,
		"required_environment": "development",
		"production_export_allowed": False,
	}


def _directory_digest(fixture_directory: Path) -> str:
	digest = hashlib.sha256()
	for path in sorted(fixture_directory.iterdir(), key=lambda item: item.name):
		if path.is_file():
			digest.update(path.name.encode())
			digest.update(b"\0")
			digest.update(path.read_bytes())
	return digest.hexdigest()


def _assert_fixture_tree_clean(target: ExportTarget, runner: CommandRunner) -> None:
	result = runner.run(
		["git", "status", "--porcelain", "--", f"{APP_NAME}/fixtures"],
		cwd=target.app_dir,
	)
	if result.stdout.strip():
		raise FixtureManagerError("Fixture files must be committed before a new export")


def _run_export_once(
	policy: FixturePolicy,
	target: ExportTarget,
	runner: CommandRunner,
) -> dict[str, object]:
	runner.run(
		["bench", "--site", target.site_name, "export-fixtures", "--app", APP_NAME],
		cwd=target.bench_dir,
	)
	report = canonicalize_fixture_repository(policy, target.fixture_directory)
	return report.as_dict()


def export_managed_fixtures(
	target: ExportTarget,
	*,
	confirmed: bool,
	correlation_id: str,
	runner: CommandRunner | None = None,
) -> dict[str, object]:
	if not confirmed:
		raise FixtureManagerError("Fixture export modifies app source; pass --yes after review")
	try:
		normalize_correlation_id(correlation_id)
	except AuditContextError as exc:
		raise FixtureManagerError("Invalid correlation ID") from exc
	command_runner = runner or CommandRunner()
	policy = load_fixture_policy()
	validate_hook_contract(policy)
	validate_development_target(target)
	_assert_fixture_tree_clean(target, command_runner)
	before_digest = _directory_digest(target.fixture_directory)
	command_runner.run(
		[
			"bench",
			"--site",
			target.site_name,
			"execute",
			"medops.services.fixtures.assert_fixture_export_allowed",
		],
		cwd=target.bench_dir,
	)
	try:
		first = _run_export_once(policy, target, command_runner)
		second = _run_export_once(policy, target, command_runner)
		if first["sha256"] != second["sha256"]:
			raise FixtureManagerError("Repeated fixture export is not idempotent")
		changed = before_digest != _directory_digest(target.fixture_directory)
		result = {
			"status": "ok",
			"changed": changed,
			"idempotent": True,
			"files": second["files"],
			"records": second["records"],
			"sha256": second["sha256"],
			"correlation_id": correlation_id,
		}
		append_audit_event(target, result=result)
		return result
	except Exception:
		append_audit_event(
			target,
			result={
				"status": "error",
				"correlation_id": correlation_id,
			},
		)
		raise


def append_audit_event(target: ExportTarget, *, result: dict[str, object]) -> None:
	event = {
		"timestamp": datetime.now(timezone.utc).isoformat(),
		"action": "export_fixtures",
		"site_name": target.site_name,
		**result,
	}
	target.audit_path.parent.mkdir(parents=True, exist_ok=True)
	with target.audit_path.open("a", encoding="utf-8") as handle:
		handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
	if os.name != "nt":
		target.audit_path.chmod(0o600)


def _correlation_id(value: str | None) -> str:
	correlation_id = value or f"COD-007-{uuid.uuid4()}"
	try:
		return normalize_correlation_id(correlation_id)
	except AuditContextError as exc:
		raise FixtureManagerError("Invalid correlation ID") from exc


def main() -> int:
	parser = argparse.ArgumentParser(description="Govern MedOps Frappe fixture exports.")
	subparsers = parser.add_subparsers(dest="command", required=True)
	subparsers.add_parser("validate", help="Validate policy, hooks and committed fixture files.")
	subparsers.add_parser("plan", help="Show the safe fixture export plan.")
	export_parser = subparsers.add_parser(
		"export",
		help="Export twice from the managed development site and verify idempotency.",
	)
	export_parser.add_argument("--bench-dir", type=Path, required=True)
	export_parser.add_argument("--site", required=True)
	export_parser.add_argument("--correlation-id")
	export_parser.add_argument("--yes", action="store_true")
	args = parser.parse_args()
	try:
		if args.command == "validate":
			result = validate_repository()
		elif args.command == "plan":
			result = build_plan(load_fixture_policy())
		else:
			target = build_export_target(args.bench_dir, args.site)
			result = export_managed_fixtures(
				target,
				confirmed=args.yes,
				correlation_id=_correlation_id(args.correlation_id),
			)
	except (FixturePolicyError, FixtureManagerError) as exc:
		print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
		return 1
	print(json.dumps(result, ensure_ascii=False, sort_keys=True))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
