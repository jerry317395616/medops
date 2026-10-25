from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from medops.common.constants import APP_NAME
from medops.common.fixture_policy import FIXTURE_DIRECTORY, load_fixture_policy
from scripts.fixture_manager import (
	CommandRunner,
	ExportTarget,
	FixtureManagerError,
	append_audit_event,
	build_export_target,
	build_plan,
	export_managed_fixtures,
	validate_development_target,
	validate_repository,
)


class FakeExportRunner(CommandRunner):
	def __init__(self, target: ExportTarget, *, unstable: bool = False) -> None:
		self.target = target
		self.unstable = unstable
		self.export_count = 0
		self.commands: list[list[str]] = []

	def run(self, command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
		self.commands.append(command)
		if command[:3] == ["git", "status", "--porcelain"]:
			return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
		if "export-fixtures" in command:
			self.export_count += 1
			suffix = str(self.export_count) if self.unstable else "stable"
			payload = [
				{
					"doctype": "Custom Field",
					"name": f"Company-medops_{suffix}",
					"module": "HRP Foundation",
					"modified": f"2026-07-29 00:00:0{self.export_count}",
				}
			]
			(self.target.fixture_directory / "1_custom_field.json").write_text(
				json.dumps(payload),
				encoding="utf-8",
			)
		return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class FixtureManagerTest(unittest.TestCase):
	def _make_target(self, root: Path, *, environment: str = "development") -> ExportTarget:
		bench_dir = root / "frappe-bench"
		app_dir = bench_dir / "apps" / APP_NAME
		fixture_directory = app_dir / APP_NAME / "fixtures"
		fixture_directory.mkdir(parents=True)
		for source in FIXTURE_DIRECTORY.iterdir():
			(fixture_directory / source.name).write_bytes(source.read_bytes())
		site_name = "hrp-dev.localhost"
		config_directory = bench_dir / "sites" / site_name
		config_directory.mkdir(parents=True)
		config = {
			"medops_environment": environment,
			"developer_mode": 1,
			"allow_tests": 1,
			"medops_external_integrations_enabled": 0,
		}
		(config_directory / "site_config.json").write_text(
			json.dumps(config),
			encoding="utf-8",
		)
		return ExportTarget(bench_dir=bench_dir, site_name=site_name, app_dir=app_dir)

	def test_plan_and_validation_expose_no_ownership_values(self) -> None:
		plan = build_plan(load_fixture_policy())
		self.assertFalse(plan["production_export_allowed"])
		self.assertNotIn("HRP System Manager", json.dumps(plan))
		result = validate_repository()
		repository = cast(dict[str, Any], result["repository"])
		self.assertEqual(repository["files"], 3)

	def test_target_must_be_same_source_checkout_and_managed_development(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			target = self._make_target(root)
			built = build_export_target(
				target.bench_dir,
				target.site_name,
				repository_root=target.app_dir,
			)
			validate_development_target(built)
			config = json.loads(target.site_config_path.read_text(encoding="utf-8"))
			config["medops_environment"] = "test"
			target.site_config_path.write_text(json.dumps(config), encoding="utf-8")
			with self.assertRaisesRegex(FixtureManagerError, "not the managed development"):
				validate_development_target(target)
			with self.assertRaisesRegex(FixtureManagerError, "source checkout"):
				build_export_target(
					target.bench_dir,
					target.site_name,
					repository_root=root / "other",
				)

	def test_production_like_site_is_rejected(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			target = self._make_target(root)
			with self.assertRaisesRegex(FixtureManagerError, "non-production"):
				build_export_target(
					target.bench_dir,
					"manager.myyr.top",
					repository_root=target.app_dir,
				)

	def test_export_requires_confirmation_and_is_idempotent_and_audited(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			target = self._make_target(Path(directory))
			runner = FakeExportRunner(target)
			with self.assertRaisesRegex(FixtureManagerError, "pass --yes"):
				export_managed_fixtures(
					target,
					confirmed=False,
					correlation_id="COD-007-test",
					runner=runner,
				)
			result = export_managed_fixtures(
				target,
				confirmed=True,
				correlation_id="COD-007-test",
				runner=runner,
			)
			self.assertTrue(result["changed"])
			self.assertTrue(result["idempotent"])
			self.assertEqual(runner.export_count, 2)
			audit = target.audit_path.read_text(encoding="utf-8")
			self.assertIn('"status": "ok"', audit)
			self.assertNotRegex(audit.lower(), r"password|token|secret")

	def test_non_idempotent_export_fails_and_records_redacted_error(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			target = self._make_target(Path(directory))
			runner = FakeExportRunner(target, unstable=True)
			with self.assertRaisesRegex(FixtureManagerError, "not idempotent"):
				export_managed_fixtures(
					target,
					confirmed=True,
					correlation_id="COD-007-unstable",
					runner=runner,
				)
			audit = target.audit_path.read_text(encoding="utf-8")
			self.assertIn('"status": "error"', audit)
			self.assertNotIn("medops_1", audit)

	def test_audit_event_contains_no_path_or_payload(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			target = self._make_target(Path(directory))
			append_audit_event(
				target,
				result={
					"status": "ok",
					"changed": False,
					"correlation_id": "COD-007-audit",
					"sha256": "a" * 64,
					"records": 0,
				},
			)
			event = json.loads(target.audit_path.read_text(encoding="utf-8"))
			self.assertNotIn("bench_dir", event)
			self.assertNotIn("payload", event)


if __name__ == "__main__":
	unittest.main()
