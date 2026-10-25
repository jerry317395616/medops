from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
	sys.path.insert(0, str(REPOSITORY_ROOT))

from medops.common.audit_context import (
	AuditContextError,
)
from medops.common.audit_context import (
	normalize_correlation_id as normalize_shared_correlation_id,
)
from medops.common.change_governance import (
	ChangedPath,
	ChangeGovernanceError,
	GovernanceReport,
	assess_changed_paths,
	inspect_change_governance,
	parse_adr_text,
	validate_adr_transition,
)

ZERO_SHA = frozenset({"", "0" * 40})


class ChangeManagerError(RuntimeError):
	"""Raised when a Git change cannot pass governance safely."""


def _run_git(root: Path, arguments: list[str], *, check: bool = True) -> str:
	result = subprocess.run(
		["git", *arguments],
		cwd=root,
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=False,
	)
	if check and result.returncode:
		raise ChangeManagerError(f"Git command failed: {arguments[0]}")
	return result.stdout.strip()


def resolve_base_commit(root: Path, base_ref: str) -> str:
	value = base_ref.strip()
	if not value or "\x00" in value or value.startswith("-"):
		raise ChangeManagerError("A safe base ref is required")
	base_commit = _run_git(root, ["rev-parse", "--verify", f"{value}^{{commit}}"])
	if re.fullmatch(r"[0-9a-f]{40}", base_commit) is None:
		raise ChangeManagerError("Base ref did not resolve to a full commit")
	ancestor = subprocess.run(
		["git", "merge-base", "--is-ancestor", base_commit, "HEAD"],
		cwd=root,
		capture_output=True,
		text=True,
		check=False,
	)
	if ancestor.returncode != 0:
		raise ChangeManagerError("Base ref must be an ancestor of HEAD")
	return base_commit


def collect_changed_paths(root: Path, base_commit: str) -> tuple[ChangedPath, ...]:
	# Audit both the removed and added paths so a package rename cannot hide
	# protected source paths behind Git's similarity heuristic.
	output = _run_git(root, ["diff", "--name-status", "--no-renames", base_commit, "--"])
	changes: dict[str, ChangedPath] = {}
	for line in output.splitlines():
		parts = line.split("\t")
		if len(parts) < 2:
			raise ChangeManagerError("Cannot parse Git name-status output")
		status = parts[0]
		if status.startswith(("R", "C")):
			raise ChangeManagerError("Renamed or copied files require delete/add changes for audit clarity")
		path = parts[1].replace("\\", "/")
		changes[path] = ChangedPath(status=status, path=path)
	untracked = _run_git(root, ["ls-files", "--others", "--exclude-standard"])
	for path in untracked.splitlines():
		normalized = path.replace("\\", "/")
		changes.setdefault(normalized, ChangedPath(status="A", path=normalized))
	return tuple(sorted(changes.values(), key=lambda item: (item.path, item.status)))


def _load_previous_adr(
	root: Path,
	base_commit: str,
	path: str,
	report: GovernanceReport,
):
	text = _run_git(root, ["show", f"{base_commit}:{path}"])
	return parse_adr_text(text + "\n", Path(path).name, report.policy)


def validate_adr_history(
	root: Path,
	base_commit: str,
	report: GovernanceReport,
	changed_paths: tuple[ChangedPath, ...],
	task_id: str,
) -> None:
	decision_by_filename = {decision.filename: decision for decision in report.decisions}
	decision_by_id = report.decision_by_id
	for changed in changed_paths:
		prefix = f"{report.policy.adr_directory}/"
		if not changed.path.startswith(prefix):
			continue
		if changed.status.startswith("D"):
			raise ChangeGovernanceError("ADR files cannot be deleted")
		filename = Path(changed.path).name
		current = decision_by_filename.get(filename)
		if current is None:
			raise ChangeGovernanceError(f"Changed ADR is not in the current registry: {filename}")
		if changed.status.startswith("A"):
			if current.status == "Accepted" and task_id not in current.task_ids:
				raise ChangeGovernanceError(f"New Accepted ADR {current.id} must link directly to {task_id}")
			continue
		previous = _load_previous_adr(root, base_commit, changed.path, report)
		validate_adr_transition(previous, current, decision_by_id, report.policy)


def build_change_plan(
	root: Path,
	base_ref: str,
	*,
	task_id: str | None = None,
) -> dict[str, object]:
	base_commit = resolve_base_commit(root, base_ref)
	report = inspect_change_governance(root)
	changed_paths = collect_changed_paths(root, base_commit)
	assessment = assess_changed_paths(report, changed_paths, task_id=task_id)
	validate_adr_history(root, base_commit, report, changed_paths, assessment.task_id)
	head_commit = _run_git(root, ["rev-parse", "HEAD"])
	return {
		"status": "ok",
		"operation": "governed-change",
		"app": report.policy.app,
		"base_commit": base_commit,
		"head_commit": head_commit,
		"governance_sha256": report.sha256,
		"assessment": assessment.as_dict(),
		"production_write_enabled": False,
		"http_write_enabled": False,
	}


def normalize_correlation_id(value: str | None) -> str:
	correlation_id = value or f"COD-008-{uuid.uuid4()}"
	try:
		return normalize_shared_correlation_id(correlation_id)
	except AuditContextError as exc:
		raise ChangeManagerError("Invalid correlation ID") from exc


def _default_audit_path(root: Path) -> Path:
	raw_git_dir = _run_git(root, ["rev-parse", "--git-dir"])
	git_dir = Path(raw_git_dir)
	if not git_dir.is_absolute():
		git_dir = root / git_dir
	return git_dir.resolve() / "medops-change-governance-audit.jsonl"


def append_audit_event(
	path: Path,
	*,
	correlation_id: str,
	status: str,
	task_id: str | None,
	governance_sha256: str | None,
	change_sha256: str | None,
	error_type: str | None = None,
) -> None:
	event = {
		"timestamp": datetime.now(timezone.utc).isoformat(),
		"action": "validate_governed_change",
		"actor_type": "github-actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local",
		"correlation_id": correlation_id,
		"status": status,
		"task_id": task_id,
		"governance_sha256": governance_sha256,
		"change_sha256": change_sha256,
		"error_type": error_type,
	}
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("a", encoding="utf-8") as handle:
		handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
	if os.name != "nt":
		path.chmod(0o600)


def validate_with_audit(
	root: Path,
	base_ref: str,
	*,
	task_id: str | None,
	correlation_id: str,
	audit_path: Path | None = None,
) -> dict[str, object]:
	target = audit_path or _default_audit_path(root)
	try:
		result = build_change_plan(root, base_ref, task_id=task_id)
	except Exception as exc:
		append_audit_event(
			target,
			correlation_id=correlation_id,
			status="error",
			task_id=task_id,
			governance_sha256=None,
			change_sha256=None,
			error_type=type(exc).__name__,
		)
		raise
	assessment = result["assessment"]
	if not isinstance(assessment, dict):
		raise ChangeManagerError("Change assessment is malformed")
	append_audit_event(
		target,
		correlation_id=correlation_id,
		status="ok",
		task_id=str(assessment["task_id"]),
		governance_sha256=str(result["governance_sha256"]),
		change_sha256=str(assessment["sha256"]),
	)
	return result


def main() -> int:
	parser = argparse.ArgumentParser(description="Validate MedOps ADR and change governance.")
	subparsers = parser.add_subparsers(dest="command", required=True)
	subparsers.add_parser("validate", help="Validate all committed ADR and change records.")
	for command in ("plan", "check"):
		change_parser = subparsers.add_parser(
			command,
			help="Assess a Git change against the declared COD record.",
		)
		change_parser.add_argument("--base-ref", required=True)
		change_parser.add_argument("--task")
		change_parser.add_argument("--correlation-id")
		change_parser.add_argument("--audit-path", type=Path)
	args = parser.parse_args()
	try:
		if args.command == "validate":
			result = inspect_change_governance(REPOSITORY_ROOT).as_public_dict()
		elif args.command == "plan":
			result = build_change_plan(REPOSITORY_ROOT, args.base_ref, task_id=args.task)
		else:
			result = validate_with_audit(
				REPOSITORY_ROOT,
				args.base_ref,
				task_id=args.task,
				correlation_id=normalize_correlation_id(args.correlation_id),
				audit_path=args.audit_path,
			)
	except (ChangeGovernanceError, ChangeManagerError) as exc:
		print(
			json.dumps(
				{"status": "error", "error_type": type(exc).__name__, "message": str(exc)},
				ensure_ascii=False,
				sort_keys=True,
			)
		)
		return 1
	print(json.dumps(result, ensure_ascii=False, sort_keys=True))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
