from __future__ import annotations

import argparse
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
from medops.common.environment_profiles import (
	EnvironmentProfile,
	EnvironmentProfileError,
	EnvironmentRegistry,
	load_environment_registry,
)

PRODUCTION_MARKERS = ("manager.myyr.top", "/prod/", "\\prod\\", "production")
PLACEHOLDER_SECRETS = frozenset({"change-me", "change-me-now", "password"})


class EnvironmentManagerError(RuntimeError):
	pass


@dataclass(frozen=True)
class EnvironmentTarget:
	profile: EnvironmentProfile
	bench_dir: Path
	site_name: str

	@property
	def audit_path(self) -> Path:
		if self.bench_dir.is_dir():
			return self.bench_dir / "logs" / "environment-audit.jsonl"
		return self.bench_dir.parent / "environment-audit.jsonl"


class CommandRunner:
	def run(
		self,
		args: list[str],
		*,
		cwd: Path,
		environment: dict[str, str] | None = None,
		capture_output: bool = False,
	) -> subprocess.CompletedProcess[str]:
		result = subprocess.run(
			args,
			cwd=cwd,
			env=environment,
			capture_output=capture_output,
			text=True,
			check=False,
		)
		if result.returncode:
			command_name = " ".join(args[:2])
			raise EnvironmentManagerError(f"Command failed: {command_name}")
		return result


def build_target(
	profile: EnvironmentProfile,
	*,
	bench_dir: str | None = None,
	site_name: str | None = None,
	allow_target_override: bool = False,
) -> EnvironmentTarget:
	default_bench = profile.expanded_bench_dir()
	target_bench = Path(bench_dir).expanduser().resolve() if bench_dir else default_bench
	target_site = site_name or profile.site_name
	if not allow_target_override and target_bench != default_bench:
		raise EnvironmentManagerError("Bench override requires --allow-target-override")
	if not allow_target_override and target_site != profile.site_name:
		raise EnvironmentManagerError("Site override requires --allow-target-override")
	if not target_site.endswith(".localhost"):
		raise EnvironmentManagerError("Non-production site names must end with .localhost")
	if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", target_site):
		raise EnvironmentManagerError("Site name contains unsupported characters")
	target_text = f"{target_bench.as_posix().lower()}/{target_site.lower()}"
	if any(marker in target_text for marker in PRODUCTION_MARKERS):
		raise EnvironmentManagerError("Production-like targets are forbidden")
	if target_bench == Path(target_bench.anchor) or target_bench == Path.home().resolve():
		raise EnvironmentManagerError("Bench target is too broad")
	return EnvironmentTarget(profile=profile, bench_dir=target_bench, site_name=target_site)


def build_plan(registry: EnvironmentRegistry, target: EnvironmentTarget) -> dict[str, object]:
	profile = target.profile
	return {
		"schema_version": registry.schema_version,
		"profile": profile.name,
		"bench_dir": str(target.bench_dir),
		"site_name": target.site_name,
		"required_apps": list(registry.required_apps),
		"site_config": profile.expected_site_config(registry.schema_version),
		"scheduler_enabled": profile.scheduler_enabled,
		"ports": profile.ports.as_dict(),
		"data_policy": "synthetic_only",
		"reset_policy": profile.reset_policy,
	}


def _site_config_path(target: EnvironmentTarget) -> Path:
	return target.bench_dir / "sites" / target.site_name / "site_config.json"


def _load_site_config(target: EnvironmentTarget) -> dict[str, Any]:
	path = _site_config_path(target)
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise EnvironmentManagerError(f"Cannot read site config for {target.site_name}") from exc
	if not isinstance(payload, dict):
		raise EnvironmentManagerError("Site config must be an object")
	return payload


def _bench_command() -> str:
	return os.environ.get("BENCH_BIN", "bench").strip() or "bench"


def _run_json(
	runner: CommandRunner,
	args: list[str],
	*,
	cwd: Path,
) -> dict[str, Any]:
	result = runner.run(args, cwd=cwd, capture_output=True)
	try:
		payload = json.loads(result.stdout)
	except json.JSONDecodeError as exc:
		raise EnvironmentManagerError(f"Command did not return JSON: {' '.join(args[:2])}") from exc
	if not isinstance(payload, dict):
		raise EnvironmentManagerError("Command JSON output must be an object")
	return payload


def _verify_locked_apps(target: EnvironmentTarget, runner: CommandRunner) -> None:
	python_bin = target.bench_dir / "env" / "bin" / "python"
	if not python_bin.is_file():
		raise EnvironmentManagerError("Bench Python executable is missing")
	runner.run(
		[
			str(python_bin),
			str(REPOSITORY_ROOT / "scripts" / "version_lock.py"),
			"--bench",
			str(target.bench_dir),
		],
		cwd=REPOSITORY_ROOT,
		capture_output=True,
	)


def _installed_apps(target: EnvironmentTarget, runner: CommandRunner) -> tuple[str, ...]:
	payload = _run_json(
		runner,
		[
			_bench_command(),
			"--site",
			target.site_name,
			"list-apps",
			"--format",
			"json",
		],
		cwd=target.bench_dir,
	)
	apps = payload.get(target.site_name)
	if not isinstance(apps, list) or not all(isinstance(app, str) for app in apps):
		raise EnvironmentManagerError("Cannot determine installed applications")
	return tuple(apps)


def _scheduler_status(target: EnvironmentTarget, runner: CommandRunner) -> str:
	payload = _run_json(
		runner,
		[
			_bench_command(),
			"--site",
			target.site_name,
			"scheduler",
			"status",
			"--format",
			"json",
		],
		cwd=target.bench_dir,
	)
	status = payload.get("status")
	if status not in {"enabled", "disabled"}:
		raise EnvironmentManagerError("Cannot determine scheduler status")
	return str(status)


def _config_drift(
	registry: EnvironmentRegistry,
	target: EnvironmentTarget,
	site_config: dict[str, Any],
) -> list[str]:
	expected = target.profile.expected_site_config(registry.schema_version)
	return [key for key, value in expected.items() if site_config.get(key) != value]


def verify_environment(
	registry: EnvironmentRegistry,
	target: EnvironmentTarget,
	*,
	runner: CommandRunner | None = None,
) -> dict[str, object]:
	command_runner = runner or CommandRunner()
	if not target.bench_dir.is_dir():
		raise EnvironmentManagerError("Bench directory does not exist")
	if not _site_config_path(target).is_file():
		raise EnvironmentManagerError("Site does not exist in the target Bench")
	_verify_locked_apps(target, command_runner)
	installed_apps = _installed_apps(target, command_runner)
	missing_apps = [app for app in registry.required_apps if app not in installed_apps]
	if missing_apps:
		raise EnvironmentManagerError(f"Site is missing required applications: {missing_apps}")
	site_config = _load_site_config(target)
	drift = _config_drift(registry, target, site_config)
	if drift:
		raise EnvironmentManagerError(f"Environment site configuration drift: {drift}")
	scheduler_status = _scheduler_status(target, command_runner)
	expected_scheduler = "enabled" if target.profile.scheduler_enabled else "disabled"
	if scheduler_status != expected_scheduler:
		raise EnvironmentManagerError(f"Scheduler must be {expected_scheduler}, got {scheduler_status}")
	return {
		"status": "ok",
		"profile": target.profile.name,
		"bench_dir": str(target.bench_dir),
		"site_name": target.site_name,
		"installed_apps": list(installed_apps),
		"scheduler": scheduler_status,
		"configuration_drift": [],
	}


def configure_environment(
	registry: EnvironmentRegistry,
	target: EnvironmentTarget,
	*,
	runner: CommandRunner | None = None,
) -> dict[str, object]:
	command_runner = runner or CommandRunner()
	if not target.bench_dir.is_dir() or not _site_config_path(target).is_file():
		raise EnvironmentManagerError("Configure requires an existing Bench and site")
	_verify_locked_apps(target, command_runner)
	installed_apps = _installed_apps(target, command_runner)
	missing_apps = [app for app in registry.required_apps if app not in installed_apps]
	if missing_apps:
		raise EnvironmentManagerError(f"Site is missing required applications: {missing_apps}")

	site_config = _load_site_config(target)
	current_profile = str(site_config.get("medops_environment") or "")
	if current_profile and current_profile != target.profile.name:
		raise EnvironmentManagerError(f"Site is already assigned to environment profile {current_profile}")
	expected_config = target.profile.expected_site_config(registry.schema_version)
	changed = bool(_config_drift(registry, target, site_config))
	for key, value in expected_config.items():
		args = [
			_bench_command(),
			"--site",
			target.site_name,
			"set-config",
			key,
			(repr(value) if isinstance(value, (bool, int)) else str(value)),
		]
		if isinstance(value, (bool, int)):
			args.append("--parse")
		command_runner.run(args, cwd=target.bench_dir)

	scheduler_state = "enable" if target.profile.scheduler_enabled else "disable"
	command_runner.run(
		[
			_bench_command(),
			"--site",
			target.site_name,
			"scheduler",
			scheduler_state,
			"--format",
			"json",
		],
		cwd=target.bench_dir,
		capture_output=True,
	)
	command_runner.run(
		[
			_bench_command(),
			"--site",
			target.site_name,
			"migrate",
			"--skip-search-index",
		],
		cwd=target.bench_dir,
	)
	result = verify_environment(registry, target, runner=command_runner)
	result["changed"] = changed
	return result


def _require_secret(name: str) -> str:
	value = os.environ.get(name, "")
	if len(value) < 12 or value.lower() in PLACEHOLDER_SECRETS:
		raise EnvironmentManagerError(f"{name} must be supplied as a non-placeholder secret")
	return value


def _redis_is_ready(port: int) -> bool:
	result = subprocess.run(
		["redis-cli", "-p", str(port), "ping"],
		capture_output=True,
		text=True,
		check=False,
	)
	return result.returncode == 0 and result.stdout.strip() == "PONG"


def _start_profile_redis(target: EnvironmentTarget) -> tuple[int, ...]:
	started: list[int] = []
	try:
		for name, port in (
			("redis_cache", target.profile.ports.redis_cache),
			("redis_queue", target.profile.ports.redis_queue),
		):
			if _redis_is_ready(port):
				continue
			config_path = target.bench_dir / "config" / f"{name}.conf"
			if not config_path.is_file():
				raise EnvironmentManagerError(f"Missing Redis configuration: {name}")
			result = subprocess.run(
				["redis-server", str(config_path), "--daemonize", "yes"],
				capture_output=True,
				text=True,
				check=False,
			)
			if result.returncode or not _redis_is_ready(port):
				raise EnvironmentManagerError(f"Cannot start environment Redis: {name}")
			started.append(port)
	except Exception:
		_stop_profile_redis(tuple(started))
		raise
	return tuple(started)


def _stop_profile_redis(ports: tuple[int, ...]) -> None:
	for port in ports:
		subprocess.run(
			["redis-cli", "-p", str(port), "shutdown", "nosave"],
			capture_output=True,
			text=True,
			check=False,
		)


def provision_environment(
	registry: EnvironmentRegistry,
	target: EnvironmentTarget,
	*,
	runner: CommandRunner | None = None,
) -> dict[str, object]:
	command_runner = runner or CommandRunner()
	created = False
	if not target.bench_dir.exists():
		_require_secret("DB_ROOT_PASSWORD")
		_require_secret("ADMIN_PASSWORD")
		environment = dict(os.environ)
		environment.update(
			{
				"BENCH_DIR": str(target.bench_dir),
				"SITE_NAME": target.site_name,
				"DEVELOPER_MODE": "1" if target.profile.developer_mode else "0",
				"KEEP_TEMPORARY_REDIS": "1",
				"REDIS_CACHE_PORT": str(target.profile.ports.redis_cache),
				"REDIS_QUEUE_PORT": str(target.profile.ports.redis_queue),
				"SOCKETIO_PORT": str(target.profile.ports.socketio),
				"WEBSERVER_PORT": str(target.profile.ports.webserver),
				"FILE_WATCHER_PORT": str(target.profile.ports.file_watcher),
			}
		)
		command_runner.run(
			["bash", str(REPOSITORY_ROOT / "scripts" / "bootstrap_latest_develop.sh")],
			cwd=REPOSITORY_ROOT,
			environment=environment,
		)
		created = True
	elif not _site_config_path(target).is_file():
		raise EnvironmentManagerError("Existing Bench does not contain the expected site")
	owned_redis_ports = (
		(target.profile.ports.redis_cache, target.profile.ports.redis_queue)
		if created
		else _start_profile_redis(target)
	)
	try:
		result = configure_environment(registry, target, runner=command_runner)
	finally:
		_stop_profile_redis(owned_redis_ports)
	result["created"] = created
	return result


def _correlation_id(value: str | None) -> str:
	correlation_id = value or f"COD-006-{uuid.uuid4()}"
	try:
		return normalize_correlation_id(correlation_id)
	except AuditContextError as exc:
		raise EnvironmentManagerError("Invalid correlation ID") from exc


def append_audit_event(
	target: EnvironmentTarget,
	*,
	correlation_id: str,
	action: str,
	status: str,
	changed: bool | None = None,
) -> None:
	event: dict[str, object] = {
		"timestamp": datetime.now(timezone.utc).isoformat(),
		"correlation_id": correlation_id,
		"profile": target.profile.name,
		"site_name": target.site_name,
		"bench_dir": str(target.bench_dir),
		"action": action,
		"status": status,
	}
	if changed is not None:
		event["changed"] = changed
	target.audit_path.parent.mkdir(parents=True, exist_ok=True)
	with target.audit_path.open("a", encoding="utf-8") as handle:
		handle.write(json.dumps(event, sort_keys=True) + "\n")
	if os.name != "nt":
		target.audit_path.chmod(0o600)


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("profile", choices=("development", "test", "demo"))
	parser.add_argument("--bench-dir")
	parser.add_argument("--site-name")
	parser.add_argument("--allow-target-override", action="store_true")


def _target_from_args(
	registry: EnvironmentRegistry,
	args: argparse.Namespace,
) -> EnvironmentTarget:
	return build_target(
		registry.get(args.profile),
		bench_dir=args.bench_dir,
		site_name=args.site_name,
		allow_target_override=args.allow_target_override,
	)


def main() -> int:
	parser = argparse.ArgumentParser(description="Manage isolated MedOps non-production environments.")
	parser.add_argument(
		"--profiles",
		type=Path,
		default=None,
		help="Override the packaged environment profile registry for validation.",
	)
	subparsers = parser.add_subparsers(dest="command", required=True)
	subparsers.add_parser("validate")
	plan_parser = subparsers.add_parser("plan")
	_add_target_arguments(plan_parser)
	for command in ("provision", "configure", "verify"):
		command_parser = subparsers.add_parser(command)
		_add_target_arguments(command_parser)
		command_parser.add_argument("--correlation-id")
	args = parser.parse_args()

	try:
		registry = load_environment_registry(args.profiles)
		if args.command == "validate":
			result: dict[str, object] = {
				"status": "ok",
				"schema_version": registry.schema_version,
				"profiles": [profile.name for profile in registry.profiles],
			}
		else:
			target = _target_from_args(registry, args)
			if args.command == "plan":
				result = build_plan(registry, target)
			else:
				correlation_id = _correlation_id(args.correlation_id)
				try:
					if args.command == "provision":
						result = provision_environment(registry, target)
					elif args.command == "configure":
						result = configure_environment(registry, target)
					else:
						result = verify_environment(registry, target)
				except Exception:
					append_audit_event(
						target,
						correlation_id=correlation_id,
						action=args.command,
						status="failed",
					)
					raise
				append_audit_event(
					target,
					correlation_id=correlation_id,
					action=args.command,
					status="success",
					changed=bool(result.get("changed")) if "changed" in result else None,
				)
				result["correlation_id"] = correlation_id
		print(json.dumps(result, ensure_ascii=False, sort_keys=True))
		return 0
	except (EnvironmentManagerError, EnvironmentProfileError) as exc:
		print(f"ENVIRONMENT ERROR: {exc}", file=sys.stderr)
		return 2


if __name__ == "__main__":
	raise SystemExit(main())
