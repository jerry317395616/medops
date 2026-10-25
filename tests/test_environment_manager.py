from __future__ import annotations

import ast
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from medops.common.environment_profiles import load_environment_registry
from scripts.environment_manager import (
	CommandRunner,
	EnvironmentManagerError,
	append_audit_event,
	build_plan,
	build_target,
	configure_environment,
	provision_environment,
)


class FakeBenchRunner(CommandRunner):
	def __init__(self, target, installed_apps=None):
		self.target = target
		self.installed_apps = installed_apps or ["frappe", "erpnext", "hrms", "medops"]
		self.scheduler = "disabled"
		self.commands: list[list[str]] = []

	def run(
		self,
		args: list[str],
		*,
		cwd: Path,
		environment: dict[str, str] | None = None,
		capture_output: bool = False,
	) -> subprocess.CompletedProcess[str]:
		self.commands.append(args)
		stdout = ""
		if "list-apps" in args:
			stdout = json.dumps({self.target.site_name: self.installed_apps})
		elif "scheduler" in args and "status" in args:
			stdout = json.dumps({"site": self.target.site_name, "status": self.scheduler})
		elif "scheduler" in args and "enable" in args:
			self.scheduler = "enabled"
			stdout = json.dumps({"site": self.target.site_name, "status": "enabled"})
		elif "scheduler" in args and "disable" in args:
			self.scheduler = "disabled"
			stdout = json.dumps({"site": self.target.site_name, "status": "disabled"})
		elif "set-config" in args:
			key_index = args.index("set-config") + 1
			key = args[key_index]
			value_text = args[key_index + 1]
			value = ast.literal_eval(value_text) if "--parse" in args else value_text
			path = self.target.bench_dir / "sites" / self.target.site_name / "site_config.json"
			payload = json.loads(path.read_text(encoding="utf-8"))
			payload[key] = value
			path.write_text(json.dumps(payload), encoding="utf-8")
		return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")


class EnvironmentManagerTest(unittest.TestCase):
	def setUp(self) -> None:
		self.registry = load_environment_registry()

	def _target(self, temp: str, profile_name: str = "test"):
		profile = self.registry.get(profile_name)
		bench_dir = Path(temp) / profile_name / "frappe-bench"
		target = build_target(
			profile,
			bench_dir=str(bench_dir),
			site_name=profile.site_name,
			allow_target_override=True,
		)
		site_dir = bench_dir / "sites" / target.site_name
		site_dir.mkdir(parents=True)
		(bench_dir / "env" / "bin").mkdir(parents=True)
		(bench_dir / "env" / "bin" / "python").write_text("", encoding="utf-8")
		(site_dir / "site_config.json").write_text("{}", encoding="utf-8")
		return target

	def test_plan_contains_no_secret_fields(self) -> None:
		profile = self.registry.get("development")
		target = build_target(profile)
		plan = build_plan(self.registry, target)
		serialized = json.dumps(plan).lower()
		self.assertNotIn("password", serialized)
		self.assertNotIn("token", serialized)
		site_config = cast(dict[str, Any], plan["site_config"])
		self.assertFalse(site_config["medops_external_integrations_enabled"])

	def test_target_override_requires_explicit_consent(self) -> None:
		profile = self.registry.get("test")
		with self.assertRaisesRegex(EnvironmentManagerError, "override requires"):
			build_target(profile, bench_dir="/tmp/medops-test")

	def test_rejects_production_like_target(self) -> None:
		profile = self.registry.get("test")
		with self.assertRaisesRegex(EnvironmentManagerError, "Production-like"):
			build_target(
				profile,
				bench_dir="/srv/medops/production/frappe-bench",
				allow_target_override=True,
			)

	def test_configure_is_idempotent_and_fails_closed_on_profile_drift(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			target = self._target(temp)
			runner = FakeBenchRunner(target)
			first = configure_environment(self.registry, target, runner=runner)
			second = configure_environment(self.registry, target, runner=runner)
			self.assertTrue(first["changed"])
			self.assertFalse(second["changed"])
			self.assertEqual(second["configuration_drift"], [])
			site_config = json.loads(
				(target.bench_dir / "sites" / target.site_name / "site_config.json").read_text(
					encoding="utf-8"
				)
			)
			self.assertEqual(site_config["medops_environment"], "test")

			site_config["medops_environment"] = "demo"
			(target.bench_dir / "sites" / target.site_name / "site_config.json").write_text(
				json.dumps(site_config), encoding="utf-8"
			)
			with self.assertRaisesRegex(EnvironmentManagerError, "already assigned"):
				configure_environment(self.registry, target, runner=runner)

	def test_missing_required_app_is_rejected_before_configuration(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			target = self._target(temp)
			runner = FakeBenchRunner(target, installed_apps=["frappe", "erpnext", "hrms"])
			with self.assertRaisesRegex(EnvironmentManagerError, "medops"):
				configure_environment(self.registry, target, runner=runner)
			self.assertFalse(any("set-config" in command for command in runner.commands))

	def test_fresh_provision_requires_non_placeholder_secrets(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			profile = self.registry.get("demo")
			target = build_target(
				profile,
				bench_dir=str(Path(temp) / "demo" / "frappe-bench"),
				allow_target_override=True,
			)
			with (
				patch.dict(os.environ, {"DB_ROOT_PASSWORD": "", "ADMIN_PASSWORD": ""}),
				self.assertRaisesRegex(EnvironmentManagerError, "DB_ROOT_PASSWORD"),
			):
				provision_environment(self.registry, target, runner=CommandRunner())

	def test_existing_provision_stops_only_redis_it_started(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			target = self._target(temp)
			with (
				patch(
					"scripts.environment_manager._start_profile_redis",
					return_value=(13200,),
				),
				patch("scripts.environment_manager._stop_profile_redis") as stop_redis,
				patch(
					"scripts.environment_manager.configure_environment",
					return_value={"status": "ok", "changed": False},
				),
			):
				result = provision_environment(self.registry, target, runner=CommandRunner())
			self.assertFalse(result["created"])
			stop_redis.assert_called_once_with((13200,))

	def test_audit_event_is_structured_and_contains_no_credentials(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			target = self._target(temp)
			append_audit_event(
				target,
				correlation_id="COD-006-audit-test",
				action="configure",
				status="success",
				changed=False,
			)
			event = json.loads(target.audit_path.read_text(encoding="utf-8"))
			self.assertEqual(event["correlation_id"], "COD-006-audit-test")
			self.assertEqual(event["status"], "success")
			serialized = json.dumps(event).lower()
			self.assertNotIn("password", serialized)
			self.assertNotIn("token", serialized)


if __name__ == "__main__":
	unittest.main()
