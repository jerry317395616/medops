from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))

from medops.common.software_supply_chain import (
	SOFTWARE_SUPPLY_CHAIN_POLICY_PATH,
	SoftwareSupplyChainContractError,
	SoftwareSupplyChainPolicy,
	compose_cyclonedx_sbom,
	evaluate_security_reports,
	load_composition_inputs,
	load_software_supply_chain_policy,
)

RAW_DIRECTORY_NAME = "raw"
SUMMARY_NAME = "security-summary.json"
CHECKSUM_NAME = "SHA256SUMS"
VERSION_PATTERN = re.compile(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9])")
INJECTED_CHILD_ENVIRONMENT = {
	"BANDIT_PROGRESS": "0",
	"GRYPE_CHECK_FOR_APP_UPDATE": "false",
	"GRYPE_DB_VALIDATE_AGE": "true",
	"NO_COLOR": "1",
	"PIP_DISABLE_PIP_VERSION_CHECK": "1",
}


class SecurityExecutionError(RuntimeError):
	"""Raised when a governed local security command cannot complete safely."""


def canonical_json(payload: object) -> str:
	return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def normalize_artifact_directory(
	value: str | Path | None,
	*,
	root: Path = ROOT,
	policy: SoftwareSupplyChainPolicy,
) -> Path:
	root = root.resolve()
	governed_root = (root / policy.execution.artifact_directory).resolve()
	candidate = governed_root if value is None else Path(value)
	if not candidate.is_absolute():
		candidate = root / candidate
	candidate = candidate.resolve()
	try:
		candidate.relative_to(governed_root)
	except ValueError as exc:
		raise SecurityExecutionError(
			f"artifact directory must remain under {policy.execution.artifact_directory}"
		) from exc
	return candidate


def resolve_executable(value: str, label: str) -> str:
	configured = value.strip()
	if not configured:
		raise SecurityExecutionError(f"{label} executable is required")
	resolved = shutil.which(configured)
	if resolved:
		return str(Path(resolved).resolve())
	path = Path(configured)
	if path.is_file():
		return str(path.resolve())
	raise SecurityExecutionError(f"{label} executable was not found")


def build_child_environment(
	policy: SoftwareSupplyChainPolicy,
	environment: dict[str, str] | None = None,
) -> dict[str, str]:
	source = dict(os.environ) if environment is None else environment
	child = {
		name: source[name] for name in policy.execution.subprocess_environment_allowlist if source.get(name)
	}
	child.update(INJECTED_CHILD_ENVIRONMENT)
	return child


def _run(
	command: Sequence[str],
	*,
	label: str,
	cwd: Path,
	environment: dict[str, str],
	timeout_seconds: int,
	allowed_exit_codes: frozenset[int] = frozenset({0}),
	stdout_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
	try:
		result = subprocess.run(
			tuple(command),
			cwd=cwd,
			env=environment,
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
			check=False,
			timeout=timeout_seconds,
		)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise SecurityExecutionError(f"{label} could not complete") from exc
	if stdout_path is not None:
		stdout_path.write_text(result.stdout, encoding="utf-8")
	if result.returncode not in allowed_exit_codes:
		raise SecurityExecutionError(f"{label} failed with exit code {result.returncode}")
	return result


def _extract_version(output: str, label: str) -> str:
	match = VERSION_PATTERN.search(output)
	if match is None:
		raise SecurityExecutionError(f"{label} did not report a semantic version")
	return match.group(1)


def verify_tool_versions(
	*,
	policy: SoftwareSupplyChainPolicy,
	npm_bin: str,
	gitleaks_bin: str,
	grype_bin: str,
	cyclonedx_bin: str,
	environment: dict[str, str],
	root: Path = ROOT,
) -> dict[str, str]:
	commands = {
		"bandit": (sys.executable, "-m", "bandit", "--version"),
		"pip_audit": (sys.executable, "-m", "pip_audit", "--version"),
		"npm": (npm_bin, "--version"),
		"gitleaks": (gitleaks_bin, "version"),
		"grype": (grype_bin, "version"),
		"cyclonedx_cli": (cyclonedx_bin, "--version"),
	}
	versions: dict[str, str] = {}
	for name, command in commands.items():
		result = _run(
			command,
			label=f"{name} version check",
			cwd=root,
			environment=environment,
			timeout_seconds=30,
		)
		version = _extract_version(result.stdout + "\n" + result.stderr, name)
		expected = policy.tool(name).version
		if version != expected:
			raise SecurityExecutionError(f"{name} {expected} is required; found {version}")
		versions[name] = version
	return versions


def _load_report(path: Path, label: str) -> Any:
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise SecurityExecutionError(f"{label} did not produce valid JSON") from exc


def redacted_gitleaks_diagnostics(report: object) -> tuple[str, ...]:
	if not isinstance(report, list):
		return ()
	diagnostics: list[str] = []
	for finding in report[:100]:
		if not isinstance(finding, dict):
			continue
		rule = re.sub(r"[^A-Za-z0-9_.-]", "?", str(finding.get("RuleID", "unknown")))[:80]
		path = re.sub(r"[^A-Za-z0-9_./-]", "?", str(finding.get("File", "unknown")))[:240]
		commit = re.sub(r"[^0-9a-f]", "", str(finding.get("Commit", "")).casefold())[:12]
		line = finding.get("StartLine")
		line_number = line if isinstance(line, int) and line > 0 else "unknown"
		diagnostics.append(
			f"GITLEAKS FINDING: rule={rule} file={path} line={line_number} commit={commit or 'unknown'}"
		)
	return tuple(diagnostics)


def _write_checksums(artifact_directory: Path, names: Sequence[str]) -> None:
	lines = []
	for name in sorted(names):
		path = artifact_directory / name
		lines.append(f"{sha256(path.read_bytes()).hexdigest()}  {name}")
	(artifact_directory / CHECKSUM_NAME).write_text("\n".join(lines) + "\n", encoding="ascii")


def build_plan(
	policy: SoftwareSupplyChainPolicy,
	*,
	artifact_directory: Path,
	source_commit: str,
) -> dict[str, object]:
	return {
		"status": "planned",
		"schema_version": policy.schema_version,
		"source_commit": source_commit,
		"policy_sha256": policy.sha256,
		"artifact_directory": artifact_directory.relative_to(ROOT.resolve()).as_posix(),
		"public_artifacts": [
			policy.sbom.artifact_name,
			SUMMARY_NAME,
			CHECKSUM_NAME,
		],
		"raw_reports_uploaded": False,
		"site_execution_enabled": policy.execution.site_execution_enabled,
		"production_execution_enabled": policy.execution.production_execution_enabled,
		"tools": {tool.name: tool.version for tool in policy.tools},
	}


def run_security_pipeline(
	*,
	source_commit: str,
	npm_bin: str,
	gitleaks_bin: str,
	grype_bin: str,
	cyclonedx_bin: str,
	artifact_directory: Path,
	policy: SoftwareSupplyChainPolicy,
	root: Path = ROOT,
	environment: dict[str, str] | None = None,
) -> dict[str, object]:
	root = root.resolve()
	artifact_directory.mkdir(parents=True, exist_ok=True)
	raw_directory = artifact_directory / RAW_DIRECTORY_NAME
	raw_directory.mkdir(parents=True, exist_ok=True)
	child_environment = build_child_environment(policy, environment)
	versions = verify_tool_versions(
		policy=policy,
		npm_bin=npm_bin,
		gitleaks_bin=gitleaks_bin,
		grype_bin=grype_bin,
		cyclonedx_bin=cyclonedx_bin,
		environment=child_environment,
		root=root,
	)

	npm_sbom_path = raw_directory / "npm.cdx.json"
	pip_audit_path = raw_directory / "pip-audit.json"
	bandit_path = raw_directory / "bandit.json"
	gitleaks_path = raw_directory / "gitleaks.json"
	npm_audit_path = raw_directory / "npm-audit.json"
	grype_path = raw_directory / "grype.json"
	final_sbom_path = artifact_directory / policy.sbom.artifact_name

	_run(
		(npm_bin, "sbom", "--sbom-format", "cyclonedx"),
		label="npm SBOM generation",
		cwd=root,
		environment=child_environment,
		timeout_seconds=180,
		stdout_path=npm_sbom_path,
	)
	_run(
		(
			sys.executable,
			"-m",
			"pip_audit",
			str(root),
			"--strict",
			"--format",
			"json",
			"--output",
			str(pip_audit_path),
		),
		label="pip-audit",
		cwd=root,
		environment=child_environment,
		timeout_seconds=240,
		allowed_exit_codes=frozenset({0, 1}),
	)
	npm_sbom, pip_audit, version_lock, package_metadata = load_composition_inputs(
		npm_sbom_path=npm_sbom_path,
		pip_audit_path=pip_audit_path,
	)
	final_sbom = compose_cyclonedx_sbom(
		npm_sbom,
		pip_audit,
		version_lock,
		package_metadata,
		source_commit=source_commit,
		policy=policy,
	)
	final_sbom_path.write_text(canonical_json(final_sbom), encoding="utf-8")
	_run(
		(
			cyclonedx_bin,
			"validate",
			"--input-file",
			str(final_sbom_path),
			"--input-format",
			"json",
			"--input-version",
			"v1_7",
			"--fail-on-errors",
		),
		label="CycloneDX validation",
		cwd=root,
		environment=child_environment,
		timeout_seconds=120,
	)
	_run(
		(
			sys.executable,
			"-m",
			"bandit",
			"-r",
			"medops",
			"scripts",
			"--severity-level",
			policy.gates.bandit_minimum_severity.casefold(),
			"--confidence-level",
			policy.gates.bandit_minimum_confidence.casefold(),
			"--format",
			"json",
			"--output",
			str(bandit_path),
			"--exit-zero",
		),
		label="Bandit",
		cwd=root,
		environment=child_environment,
		timeout_seconds=180,
	)
	_run(
		(
			gitleaks_bin,
			"git",
			"--redact",
			"--no-banner",
			"--report-format",
			"json",
			"--report-path",
			str(gitleaks_path),
			"--gitleaks-ignore-path",
			".gitleaksignore",
			"--exit-code",
			"1",
			".",
		),
		label="Gitleaks",
		cwd=root,
		environment=child_environment,
		timeout_seconds=240,
		allowed_exit_codes=frozenset({0, 1}),
	)
	if not gitleaks_path.is_file():
		gitleaks_path.write_text("[]\n", encoding="utf-8")
	_run(
		(npm_bin, "audit", "--json"),
		label="npm audit",
		cwd=root,
		environment=child_environment,
		timeout_seconds=180,
		allowed_exit_codes=frozenset({0, 1}),
		stdout_path=npm_audit_path,
	)
	_run(
		(
			grype_bin,
			f"sbom:{final_sbom_path}",
			"--quiet",
			"--output",
			"json",
			"--file",
			str(grype_path),
		),
		label="Grype",
		cwd=root,
		environment=child_environment,
		timeout_seconds=420,
	)
	gitleaks_report = _load_report(gitleaks_path, "Gitleaks")
	summary = evaluate_security_reports(
		policy=policy,
		sbom=final_sbom,
		bandit_report=_load_report(bandit_path, "Bandit"),
		gitleaks_report=gitleaks_report,
		pip_audit_report=_load_report(pip_audit_path, "pip-audit"),
		npm_audit_report=_load_report(npm_audit_path, "npm audit"),
		grype_report=_load_report(grype_path, "Grype"),
		source_commit=source_commit,
	)
	if summary["tools"] != versions:
		raise SecurityExecutionError("verified tool versions differ from the security summary")
	summary_path = artifact_directory / SUMMARY_NAME
	summary_path.write_text(canonical_json(summary), encoding="utf-8")
	_write_checksums(
		artifact_directory,
		(policy.sbom.artifact_name, SUMMARY_NAME),
	)
	if not summary["passed"]:
		violations = summary["violations"]
		if not isinstance(violations, list):
			raise SecurityExecutionError("security summary violations are invalid")
		if "secret_findings" in violations:
			for diagnostic in redacted_gitleaks_diagnostics(gitleaks_report):
				print(diagnostic, file=sys.stderr)
		raise SecurityExecutionError("security gates failed: " + ",".join(str(item) for item in violations))
	return summary


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description="Generate and evaluate the governed medops SBOM and security reports"
	)
	parser.add_argument("--policy", type=Path, default=SOFTWARE_SUPPLY_CHAIN_POLICY_PATH)
	subparsers = parser.add_subparsers(dest="command", required=True)
	for command in ("plan", "run"):
		subparser = subparsers.add_parser(command)
		subparser.add_argument("--source-commit", required=True)
		subparser.add_argument("--artifact-directory", type=Path)
		if command == "run":
			subparser.add_argument("--npm-bin", default="npm")
			subparser.add_argument("--gitleaks-bin", required=True)
			subparser.add_argument("--grype-bin", required=True)
			subparser.add_argument("--cyclonedx-bin", required=True)
	return parser


def main(argv: Sequence[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	try:
		policy = load_software_supply_chain_policy(args.policy)
		artifact_directory = normalize_artifact_directory(
			args.artifact_directory,
			policy=policy,
		)
		if args.command == "plan":
			print(
				canonical_json(
					build_plan(
						policy,
						artifact_directory=artifact_directory,
						source_commit=args.source_commit,
					)
				),
				end="",
			)
			return 0
		summary = run_security_pipeline(
			source_commit=args.source_commit,
			npm_bin=resolve_executable(args.npm_bin, "npm"),
			gitleaks_bin=resolve_executable(args.gitleaks_bin, "Gitleaks"),
			grype_bin=resolve_executable(args.grype_bin, "Grype"),
			cyclonedx_bin=resolve_executable(args.cyclonedx_bin, "CycloneDX CLI"),
			artifact_directory=artifact_directory,
			policy=policy,
		)
	except (SecurityExecutionError, SoftwareSupplyChainContractError) as exc:
		print(f"SECURITY PIPELINE FAILED: {exc}", file=sys.stderr)
		return 1
	print(canonical_json(summary), end="")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
