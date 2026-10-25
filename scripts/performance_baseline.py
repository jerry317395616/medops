from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from medops.common.performance_baseline import (
	PerformanceBaselineContractError,
	evaluate_performance_run,
	load_performance_baseline_registry,
	parse_performance_run_summary,
)

CONFIRMATION = "NON_PRODUCTION_LOAD_TEST"
HOST_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PERFORMANCE_ARTIFACT_ROOT = PROJECT_ROOT / ".artifacts" / "performance"
CHILD_ENVIRONMENT_ALLOWLIST = frozenset(
	{
		"HOME",
		"HTTPS_PROXY",
		"HTTP_PROXY",
		"LANG",
		"LC_ALL",
		"NO_PROXY",
		"PATH",
		"SSL_CERT_DIR",
		"SSL_CERT_FILE",
		"SYSTEMROOT",
		"TEMP",
		"TMP",
		"USERPROFILE",
		"WINDIR",
	}
)


def normalize_base_url(value: str) -> str:
	parsed = urlsplit(value)
	if (
		parsed.username
		or parsed.password
		or parsed.query
		or parsed.fragment
		or parsed.path not in {"", "/"}
		or not parsed.hostname
		or not HOST_PATTERN.fullmatch(parsed.hostname)
	):
		raise ValueError("base URL must contain only scheme, host, and optional port")
	is_loopback = parsed.hostname in {"localhost", "127.0.0.1"}
	if parsed.scheme == "http" and not is_loopback:
		raise ValueError("non-loopback performance targets must use HTTPS")
	if parsed.scheme not in {"http", "https"}:
		raise ValueError("performance target must use HTTP or HTTPS")
	try:
		port = parsed.port
	except ValueError as exc:
		raise ValueError("base URL port is invalid") from exc
	if port is not None and not 1 <= port <= 65535:
		raise ValueError("base URL port is invalid")
	netloc = parsed.hostname if port is None else f"{parsed.hostname}:{port}"
	return f"{parsed.scheme}://{netloc}"


def build_plan(base_url: str, scenario_id: str, profile_name: str) -> dict[str, object]:
	registry = load_performance_baseline_registry()
	scenario = registry.get(scenario_id)
	profile = scenario.get_profile(profile_name)
	return {
		"status": "planned",
		"schema_version": registry.schema_version,
		"registry_sha256": registry.sha256,
		"tool": {"name": "k6", "version": registry.policy.k6_version},
		"scenario_id": scenario.scenario_id,
		"scenario_version": scenario.version,
		"profile": profile.profile,
		"target": {
			"scheme": urlsplit(base_url).scheme,
			"loopback": urlsplit(base_url).hostname in {"localhost", "127.0.0.1"},
		},
		"method": scenario.method,
		"path": scenario.path,
		"virtual_users": profile.virtual_users,
		"iterations": profile.iterations,
		"max_duration_seconds": profile.max_duration_seconds,
		"thresholds": profile.thresholds.as_public_dict(),
		"requires_contract_gate": True,
		"persists_credentials": False,
		"persists_target_url": False,
	}


def normalize_output_path(value: str) -> Path:
	artifact_root = PERFORMANCE_ARTIFACT_ROOT.resolve()
	candidate = Path(value).expanduser()
	if not candidate.is_absolute():
		candidate = PROJECT_ROOT / candidate
	output_path = candidate.resolve()
	if (
		not output_path.is_relative_to(artifact_root)
		or output_path.suffix.lower() != ".json"
		or output_path == artifact_root
	):
		raise ValueError("output must be a JSON file inside .artifacts/performance")
	return output_path


def build_child_environment(
	parent_environment: Mapping[str, str],
	overrides: Mapping[str, str],
) -> dict[str, str]:
	child_environment = {
		key: value for key, value in parent_environment.items() if key.upper() in CHILD_ENVIRONMENT_ALLOWLIST
	}
	child_environment.update(overrides)
	return child_environment


def _required_secret(name: str) -> str:
	value = os.environ.get(name, "")
	if not value or any(ord(character) < 33 for character in value):
		raise ValueError(f"{name} must be set as a non-empty environment variable")
	return value


def execute(args: argparse.Namespace) -> int:
	base_url = normalize_base_url(args.base_url)
	registry = load_performance_baseline_registry()
	scenario = registry.get(args.scenario)
	profile = scenario.get_profile(args.profile)
	plan = build_plan(base_url, scenario.scenario_id, profile.profile)
	if args.dry_run:
		print(json.dumps(plan, ensure_ascii=False, indent=2))
		return 0
	if os.environ.get("IONE_PERF_CONFIRM") != CONFIRMATION:
		raise ValueError(f"IONE_PERF_CONFIRM must equal {CONFIRMATION}")
	api_key = _required_secret("IONE_PERF_API_KEY")
	api_secret = _required_secret("IONE_PERF_API_SECRET")
	k6_binary = shutil.which(args.k6_binary)
	if not k6_binary:
		raise ValueError("k6 executable was not found")
	version_process = subprocess.run(
		[k6_binary, "version"],
		check=False,
		capture_output=True,
		text=True,
	)
	version_output = f"{version_process.stdout}\n{version_process.stderr}"
	version_match = re.search(r"\bv([0-9]+\.[0-9]+\.[0-9]+)\b", version_output)
	if (
		version_process.returncode != 0
		or version_match is None
		or version_match.group(1) != registry.policy.k6_version
	):
		raise ValueError(f"k6 {registry.policy.k6_version} is required")
	output_path = normalize_output_path(args.output)
	output_path.parent.mkdir(parents=True, exist_ok=True)
	script_path = PROJECT_ROOT / "medops" / "load_tests" / "performance_baseline.js"
	run_id = f"perf-{uuid.uuid4().hex[:24]}"
	with tempfile.TemporaryDirectory(prefix="medops-performance-") as temp:
		raw_summary_path = Path(temp) / "k6-summary.json"
		child_environment = build_child_environment(
			os.environ,
			{
				"IONE_PERF_CONFIRM": CONFIRMATION,
				"IONE_PERF_BASE_URL": base_url,
				"IONE_PERF_API_KEY": api_key,
				"IONE_PERF_API_SECRET": api_secret,
				"IONE_PERF_SCENARIO": scenario.scenario_id,
				"IONE_PERF_PROFILE": profile.profile,
				"IONE_PERF_REGISTRY_SHA256": registry.sha256,
				"IONE_PERF_RUN_ID": run_id,
				"IONE_PERF_SUMMARY_PATH": str(raw_summary_path),
			},
		)
		completed = subprocess.run(
			[k6_binary, "run", str(script_path)],
			env=child_environment,
			check=False,
		)
		if not raw_summary_path.is_file():
			raise ValueError("k6 did not produce a governed summary")
		raw_summary = json.loads(raw_summary_path.read_text(encoding="utf-8"))
	summary = parse_performance_run_summary(raw_summary)
	evaluation = evaluate_performance_run(summary, registry)
	report = {
		"schema_version": 1,
		"summary": summary.as_dict(),
		"evaluation": evaluation,
	}
	output_path.write_text(
		json.dumps(report, ensure_ascii=False, indent=2) + "\n",
		encoding="utf-8",
	)
	print(json.dumps(evaluation, ensure_ascii=False))
	return 0 if completed.returncode == 0 and evaluation["passed"] else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Run the governed MedOps non-production k6 performance baseline."
	)
	parser.add_argument("--base-url", required=True)
	parser.add_argument("--scenario", default="platform-module-registry-read")
	parser.add_argument("--profile", choices=("smoke", "baseline", "load"), default="smoke")
	parser.add_argument("--output", default=".artifacts/performance/latest.json")
	parser.add_argument("--k6-binary", default="k6")
	parser.add_argument("--dry-run", action="store_true")
	return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
	try:
		return execute(parse_args(argv))
	except (
		PerformanceBaselineContractError,
		ValueError,
		OSError,
		json.JSONDecodeError,
	) as exc:
		print(f"performance baseline failed: {exc}", file=sys.stderr)
		return 2


if __name__ == "__main__":
	raise SystemExit(main())
