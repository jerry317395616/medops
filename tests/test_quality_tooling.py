from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.quality import (
	QualityConfigurationError,
	build_quality_steps,
	resolve_npm,
	run_quality,
)
from scripts.repository_contract import validate_quality_tooling

ROOT = Path(__file__).resolve().parents[1]
QUALITY_FILES = (
	".npmrc",
	".prettierignore",
	".prettierrc.json",
	"eslint.config.mjs",
	"package-lock.json",
	"package.json",
	"pyproject.toml",
	"pyrightconfig.json",
	"scripts/quality.py",
	"scripts/quality.sh",
	"scripts/tests/reservation_session.test.mjs",
	"scripts/tests/reservation_page.test.mjs",
)


class _Result:
	def __init__(self, returncode: int) -> None:
		self.returncode = returncode


def _digest(path: Path) -> str:
	return hashlib.sha256(path.read_bytes()).hexdigest()


class QualityToolingTest(unittest.TestCase):
	def test_current_configuration_satisfies_contract(self) -> None:
		self.assertEqual(validate_quality_tooling(ROOT), [])

	def test_quality_plan_is_complete_and_deterministic(self) -> None:
		expected = (
			"repository-contract",
			"package-validation",
			"python-compile",
			"ruff-lint",
			"ruff-format",
			"bandit",
			"unit-tests",
			"checksums",
			"pyright",
			"eslint",
			"frontend-tests",
			"prettier",
		)
		first = build_quality_steps(python_executable="python", npm_executable="npm")
		second = build_quality_steps(python_executable="python", npm_executable="npm")
		self.assertEqual(tuple(step.name for step in first), expected)
		self.assertEqual(first, second)
		pyright_step = next(step for step in first if step.name == "pyright")
		self.assertEqual(pyright_step.command[-2:], ("--pythonpath", "python"))

	def test_missing_npm_is_an_explicit_error(self) -> None:
		with (
			patch("scripts.quality.shutil.which", return_value=None),
			self.assertRaisesRegex(QualityConfigurationError, "npm is required"),
		):
			resolve_npm({})

	def test_failed_step_stops_the_quality_run(self) -> None:
		calls: list[tuple[str, ...]] = []

		def failing_runner(command, **_kwargs):
			calls.append(tuple(command))
			return _Result(7)

		with (
			redirect_stdout(io.StringIO()),
			self.assertRaisesRegex(QualityConfigurationError, "repository-contract.*exit code 7"),
		):
			run_quality(
				root=ROOT,
				runner=failing_runner,
				python_executable="python",
				npm_executable="npm",
			)
		self.assertEqual(len(calls), 1)

	def test_read_only_quality_plan_does_not_mutate_configuration(self) -> None:
		before = {path: _digest(ROOT / path) for path in QUALITY_FILES}

		def successful_runner(_command, **_kwargs):
			return _Result(0)

		with redirect_stdout(io.StringIO()):
			completed = run_quality(
				root=ROOT,
				runner=successful_runner,
				python_executable="python",
				npm_executable="npm",
			)
		self.assertEqual(len(completed), 12)
		self.assertEqual(
			{path: _digest(ROOT / path) for path in QUALITY_FILES},
			before,
		)

	def test_unpinned_dependency_is_rejected(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			for relative_path in QUALITY_FILES:
				target = root / relative_path
				target.parent.mkdir(parents=True, exist_ok=True)
				target.write_bytes((ROOT / relative_path).read_bytes())
			package_path = root / "package.json"
			package = json.loads(package_path.read_text(encoding="utf-8"))
			package["devDependencies"]["eslint"] = "^10.8.0"
			package_path.write_text(json.dumps(package), encoding="utf-8")

			violations = validate_quality_tooling(root)
			self.assertIn(
				"quality dependency must use an exact version: eslint=^10.8.0",
				violations,
			)
			self.assertIn(
				"package-lock.json root devDependencies differ from package.json",
				violations,
			)

	def test_dropped_frontend_execution_gate_is_rejected(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			for relative_path in QUALITY_FILES:
				target = root / relative_path
				target.parent.mkdir(parents=True, exist_ok=True)
				target.write_bytes((ROOT / relative_path).read_bytes())
			package_path = root / "package.json"
			package = json.loads(package_path.read_text(encoding="utf-8"))
			package["scripts"]["test:frontend"] = "node -e ''"
			package_path.write_text(json.dumps(package), encoding="utf-8")
			self.assertIn(
				"frontend tests must run the actual Node test suites", validate_quality_tooling(root)
			)


if __name__ == "__main__":
	unittest.main()
