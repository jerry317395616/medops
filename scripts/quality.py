from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parents[1]
PYTHON_TARGETS = ("medops", "scripts", "tests")


class ProcessResult(Protocol):
	returncode: int


Runner = Callable[..., ProcessResult]


class QualityConfigurationError(RuntimeError):
	"""Raised when the pinned quality toolchain cannot be executed."""


@dataclass(frozen=True, slots=True)
class QualityStep:
	name: str
	command: tuple[str, ...]


def resolve_npm(environment: dict[str, str] | None = None) -> str:
	environment = dict(os.environ) if environment is None else environment
	configured = environment.get("NPM_BIN", "").strip()
	if configured:
		resolved = shutil.which(configured)
		if resolved:
			return resolved
		configured_path = Path(configured)
		if configured_path.is_file():
			return str(configured_path.resolve())
		raise QualityConfigurationError(f"NPM_BIN does not exist or is not executable: {configured}")

	candidates = ("npm.cmd", "npm") if os.name == "nt" else ("npm", "npm.cmd")
	for candidate in candidates:
		resolved = shutil.which(candidate)
		if resolved:
			return resolved
	raise QualityConfigurationError("npm is required; install Node.js or set NPM_BIN")


def build_quality_steps(
	*,
	python_executable: str,
	npm_executable: str,
	mode: str = "all",
) -> tuple[QualityStep, ...]:
	python_steps = (
		QualityStep(
			"repository-contract",
			(python_executable, "scripts/repository_contract.py"),
		),
		QualityStep(
			"package-validation",
			(python_executable, "scripts/validate_package.py"),
		),
		QualityStep(
			"python-compile",
			(python_executable, "-m", "compileall", "-q", *PYTHON_TARGETS),
		),
		QualityStep(
			"ruff-lint",
			(python_executable, "-m", "ruff", "check", *PYTHON_TARGETS),
		),
		QualityStep(
			"ruff-format",
			(python_executable, "-m", "ruff", "format", "--check", *PYTHON_TARGETS),
		),
		QualityStep(
			"bandit",
			(
				python_executable,
				"-m",
				"bandit",
				"-r",
				"medops",
				"scripts",
				"--severity-level",
				"medium",
				"--confidence-level",
				"medium",
				"--quiet",
			),
		),
		QualityStep(
			"unit-tests",
			(
				python_executable,
				"-m",
				"unittest",
				"discover",
				"-s",
				"tests",
				"-p",
				"test_*.py",
			),
		),
		QualityStep(
			"checksums",
			(python_executable, "scripts/checksums.py"),
		),
	)
	node_steps = (
		QualityStep(
			"pyright",
			(npm_executable, "run", "typecheck", "--", "--pythonpath", python_executable),
		),
		QualityStep("eslint", (npm_executable, "run", "lint:frontend")),
		QualityStep("prettier", (npm_executable, "run", "format:frontend:check")),
	)
	if mode == "python":
		return python_steps
	if mode == "node":
		return node_steps
	if mode == "all":
		return (*python_steps, *node_steps)
	raise QualityConfigurationError(f"Unknown quality mode: {mode}")


def run_quality(
	*,
	root: Path = ROOT,
	mode: str = "all",
	runner: Runner = subprocess.run,
	python_executable: str = sys.executable,
	npm_executable: str | None = None,
) -> tuple[str, ...]:
	root = root.resolve()
	required_files = (
		root / "pyproject.toml",
		root / "pyrightconfig.json",
		root / "package.json",
		root / "package-lock.json",
		root / "eslint.config.mjs",
		root / ".prettierrc.json",
	)
	missing = [str(path.relative_to(root)) for path in required_files if not path.is_file()]
	if missing:
		raise QualityConfigurationError("Missing quality configuration: " + ", ".join(missing))

	npm = npm_executable or resolve_npm()
	completed: list[str] = []
	for step in build_quality_steps(
		python_executable=python_executable,
		npm_executable=npm,
		mode=mode,
	):
		print(f"[quality] {step.name}: {' '.join(step.command)}", flush=True)
		result = runner(step.command, cwd=root, check=False)
		if result.returncode:
			raise QualityConfigurationError(
				f"Quality step {step.name!r} failed with exit code {result.returncode}"
			)
		completed.append(step.name)
	return tuple(completed)


def main(argv: Sequence[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Run the pinned medops quality toolchain")
	parser.add_argument("--mode", choices=("all", "python", "node"), default="all")
	args = parser.parse_args(argv)
	try:
		completed = run_quality(mode=args.mode)
	except QualityConfigurationError as exc:
		print(f"QUALITY FAILED: {exc}", file=sys.stderr)
		return 1
	print("QUALITY PASSED: " + ", ".join(completed))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
