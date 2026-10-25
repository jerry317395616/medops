from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "resolved_versions.lock.json"
UPSTREAM_APPS = ("frappe", "erpnext", "hrms")
EXPECTED_REPOSITORIES = {
	"frappe": "https://github.com/jerry317395616/frappe.git",
	"erpnext": "https://github.com/jerry317395616/erpnext.git",
	"hrms": "https://github.com/jerry317395616/hrms.git",
}
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
VERSION_PATTERN = re.compile(r'__version__\s*=\s*["\']([^"\']+)')


class VersionLockError(ValueError):
	pass


def normalize_repository(value: str) -> str:
	normalized = value.strip().removesuffix("/").removesuffix(".git")
	if normalized.startswith("git@github.com:"):
		normalized = f"https://github.com/{normalized.removeprefix('git@github.com:')}"
	return normalized.lower()


def validate_lock(payload: dict[str, Any]) -> list[str]:
	issues: list[str] = []
	if payload.get("schema_version") != 1:
		issues.append("schema_version must be 1")

	apps = payload.get("apps")
	if not isinstance(apps, dict):
		return [*issues, "apps must be an object"]
	if set(apps) != set(UPSTREAM_APPS):
		issues.append(f"apps must be exactly {list(UPSTREAM_APPS)}")

	for app in UPSTREAM_APPS:
		row = apps.get(app)
		if not isinstance(row, dict):
			issues.append(f"{app} lock row is missing")
			continue
		repository = str(row.get("repository") or "")
		if normalize_repository(repository) != normalize_repository(EXPECTED_REPOSITORIES[app]):
			issues.append(f"{app} repository must be {EXPECTED_REPOSITORIES[app]}")
		branch = str(row.get("branch") or "")
		if branch != "develop" and not re.fullmatch(r"medops-v17-[0-9]{8}", branch):
			issues.append(f"{app} branch must be develop or a dated MedOps source branch")
		commit = str(row.get("commit") or "")
		if not SHA_PATTERN.fullmatch(commit):
			issues.append(f"{app} commit must be a lowercase 40-character SHA")
		version = str(row.get("version") or "")
		if not version.startswith("17."):
			issues.append(f"{app} version must be in the v17 family")
	return issues


def load_lock(path: Path = DEFAULT_LOCK) -> dict[str, Any]:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except FileNotFoundError as exc:
		raise VersionLockError(f"version lock not found: {path}") from exc
	except json.JSONDecodeError as exc:
		raise VersionLockError(f"invalid version lock JSON: {exc}") from exc
	issues = validate_lock(payload)
	if issues:
		raise VersionLockError("; ".join(issues))
	return payload


def run_git(app_root: Path, *args: str, required: bool = True) -> str | None:
	result = subprocess.run(
		["git", *args],
		cwd=app_root,
		text=True,
		capture_output=True,
		check=False,
	)
	if result.returncode:
		if not required:
			return None
		message = result.stderr.strip() or result.stdout.strip() or "git command failed"
		raise VersionLockError(f"{app_root}: {message}")
	return result.stdout.strip()


def read_version_marker(app_root: Path, app: str) -> str | None:
	init_file = app_root / app / "__init__.py"
	if not init_file.is_file():
		return None
	match = VERSION_PATTERN.search(init_file.read_text(encoding="utf-8"))
	return match.group(1) if match else None


def read_app_state(app_root: Path, app: str) -> dict[str, Any]:
	branch = run_git(app_root, "branch", "--show-current", required=False)
	remote_names = (run_git(app_root, "remote", required=False) or "").splitlines()
	remote_name = (
		"origin"
		if "origin" in remote_names
		else "upstream"
		if "upstream" in remote_names
		else remote_names[0]
		if remote_names
		else None
	)
	remote = run_git(app_root, "remote", "get-url", remote_name, required=False) if remote_name else None
	dirty_output = run_git(app_root, "status", "--porcelain")
	return {
		"commit": run_git(app_root, "rev-parse", "HEAD"),
		"branch": branch or "DETACHED",
		"remote_name": remote_name,
		"repository": remote,
		"version": read_version_marker(app_root, app),
		"dirty": bool(dirty_output),
	}


def compare_bench(
	lock: dict[str, Any],
	bench: Path,
	*,
	require_clean: bool = True,
) -> dict[str, Any]:
	states: dict[str, dict[str, Any]] = {}
	issues: list[str] = []
	for app in UPSTREAM_APPS:
		app_root = bench / "apps" / app
		if not app_root.is_dir():
			issues.append(f"{app}: missing {app_root}")
			continue
		try:
			state = read_app_state(app_root, app)
		except VersionLockError as exc:
			issues.append(f"{app}: {exc}")
			continue
		states[app] = state
		expected = lock["apps"][app]
		if state["commit"] != expected["commit"]:
			issues.append(f"{app}: commit mismatch, expected {expected['commit']}, got {state['commit']}")
		if state["version"] != expected["version"]:
			issues.append(f"{app}: version mismatch, expected {expected['version']}, got {state['version']}")
		if require_clean and state["dirty"]:
			issues.append(f"{app}: worktree is dirty")
		if not state["repository"]:
			issues.append(f"{app}: Git remote is unavailable")
		elif normalize_repository(str(state["repository"])) != normalize_repository(
			str(expected["repository"])
		):
			issues.append(
				f"{app}: remote mismatch, expected {expected['repository']}, got {state['repository']}"
			)
	return {
		"status": "ok" if not issues else "mismatch",
		"bench": str(bench.resolve()),
		"apps": states,
		"issues": issues,
	}


def read_remote_head(repository: str, branch: str) -> str:
	result = subprocess.run(
		["git", "ls-remote", repository, f"refs/heads/{branch}"],
		text=True,
		capture_output=True,
		check=False,
	)
	if result.returncode:
		raise VersionLockError(result.stderr.strip() or f"cannot read {repository}")
	rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
	if len(rows) != 1 or not SHA_PATTERN.fullmatch(rows[0][0]):
		raise VersionLockError(f"unexpected ls-remote result for {repository} {branch}")
	return rows[0][0]


def compare_remote_heads(lock: dict[str, Any]) -> dict[str, Any]:
	apps: dict[str, dict[str, Any]] = {}
	issues: list[str] = []
	for app in UPSTREAM_APPS:
		expected = lock["apps"][app]
		actual = read_remote_head(expected["repository"], expected["branch"])
		matches = actual == expected["commit"]
		apps[app] = {
			"locked": expected["commit"],
			"remote_head": actual,
			"matches": matches,
		}
		if not matches:
			issues.append(f"{app}: personal fork develop has advanced to {actual}")
	return {"status": "ok" if not issues else "advanced", "apps": apps, "issues": issues}


def main() -> int:
	parser = argparse.ArgumentParser(description="Validate the immutable Frappe ecosystem commit lock.")
	parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
	parser.add_argument("--bench", type=Path)
	parser.add_argument(
		"--allow-dirty",
		action="store_true",
		help="Do not reject dirty upstream worktrees.",
	)
	parser.add_argument(
		"--verify-remote-heads",
		action="store_true",
		help="Check whether the lock still equals the personal fork develop heads.",
	)
	args = parser.parse_args()

	try:
		lock = load_lock(args.lock)
		reports: dict[str, Any] = {
			"status": "ok",
			"lock": str(args.lock.resolve()),
			"apps": list(UPSTREAM_APPS),
		}
		if args.bench:
			bench_report = compare_bench(
				lock,
				args.bench.resolve(),
				require_clean=not args.allow_dirty,
			)
			reports["bench"] = bench_report
			if bench_report["status"] != "ok":
				reports["status"] = "mismatch"
		if args.verify_remote_heads:
			remote_report = compare_remote_heads(lock)
			reports["remote_heads"] = remote_report
			if remote_report["status"] != "ok":
				reports["status"] = "advanced"
	except VersionLockError as exc:
		print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
		return 2

	print(json.dumps(reports, ensure_ascii=False, indent=2))
	return 0 if reports["status"] == "ok" else 1


if __name__ == "__main__":
	raise SystemExit(main())
