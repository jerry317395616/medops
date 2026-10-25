from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast

from medops.common.change_governance import ChangeGovernanceError
from scripts.change_manager import (
	ChangeManagerError,
	build_change_plan,
	collect_changed_paths,
	normalize_correlation_id,
	resolve_base_commit,
	validate_with_audit,
)

ROOT = Path(__file__).resolve().parents[1]


def _git(root: Path, *arguments: str) -> str:
	result = subprocess.run(
		["git", *arguments],
		cwd=root,
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=True,
	)
	return result.stdout.strip()


class TestChangeManager(unittest.TestCase):
	def setUp(self) -> None:
		self.temporary = tempfile.TemporaryDirectory()
		self.root = Path(self.temporary.name)
		for relative in (
			"medops/config/change_governance.json",
			"medops/modules.txt",
			"backlog/backlog.csv",
			"pyproject.toml",
		):
			target = self.root / relative
			target.parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(ROOT / relative, target)
		for directory in ("architecture/adr", "changes"):
			shutil.copytree(ROOT / directory, self.root / directory)
		(self.root / "backlog").mkdir(exist_ok=True)
		for task_document in sorted((ROOT / "backlog").glob("COD-*.md")):
			shutil.copy2(task_document, self.root / "backlog" / task_document.name)
		_git(self.root, "init", "-b", "main")
		_git(self.root, "config", "user.name", "COD Test")
		_git(self.root, "config", "user.email", "cod-test@example.invalid")
		_git(self.root, "add", ".")
		_git(self.root, "commit", "-m", "baseline")
		self.base_commit = _git(self.root, "rev-parse", "HEAD")
		self._create_governed_diff()

	def tearDown(self) -> None:
		self.temporary.cleanup()

	def _touch(self, relative: str, suffix: str = "\n") -> None:
		path = self.root / relative
		path.write_text(path.read_text(encoding="utf-8") + suffix, encoding="utf-8")

	def _create_governed_diff(self) -> None:
		self._touch("pyproject.toml", "\n# COD-008 governed test\n")
		self._touch("changes/COD-008.json")
		self._touch("backlog/COD-008.md")
		self._touch("backlog/backlog.csv")

	def test_plan_is_deterministic_and_uses_full_commits(self) -> None:
		first = build_change_plan(self.root, self.base_commit, task_id="COD-008")
		second = build_change_plan(self.root, self.base_commit, task_id="COD-008")

		self.assertEqual(first, second)
		self.assertEqual(first["base_commit"], self.base_commit)
		first_assessment = cast(dict[str, object], first["assessment"])
		self.assertEqual(first_assessment["required_risk"], "critical")
		self.assertTrue(first_assessment["idempotent"])
		self.assertFalse(first["production_write_enabled"])
		self.assertFalse(first["http_write_enabled"])

	def test_rename_is_audited_as_deleted_and_added_paths(self) -> None:
		(self.root / "old-package.txt").write_text("package identity\n", encoding="utf-8")
		_git(self.root, "add", "old-package.txt")
		_git(self.root, "commit", "-m", "rename fixture")
		base = _git(self.root, "rev-parse", "HEAD")
		_git(self.root, "mv", "old-package.txt", "new-package.txt")
		changes = {(row.status, row.path) for row in collect_changed_paths(self.root, base)}
		self.assertIn(("D", "old-package.txt"), changes)
		self.assertIn(("A", "new-package.txt"), changes)

	def test_check_writes_redacted_append_only_audit(self) -> None:
		audit_path = self.root / ".git/change-governance-audit.jsonl"
		first = validate_with_audit(
			self.root,
			self.base_commit,
			task_id="COD-008",
			correlation_id="COD-008-audit-1",
			audit_path=audit_path,
		)
		second = validate_with_audit(
			self.root,
			self.base_commit,
			task_id="COD-008",
			correlation_id="COD-008-audit-2",
			audit_path=audit_path,
		)
		events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line]

		first_assessment = cast(dict[str, object], first["assessment"])
		second_assessment = cast(dict[str, object], second["assessment"])
		self.assertEqual(first_assessment["sha256"], second_assessment["sha256"])
		self.assertEqual(len(events), 2)
		self.assertEqual({event["status"] for event in events}, {"ok"})
		serialized = json.dumps(events)
		self.assertNotIn(str(self.root), serialized)
		self.assertNotIn("password", serialized.lower())
		self.assertNotIn("token", serialized.lower())
		if os.name != "nt":
			self.assertEqual(stat.S_IMODE(audit_path.stat().st_mode), 0o600)

	def test_failed_check_is_audited_without_error_payload(self) -> None:
		(self.root / "unapproved.txt").write_text("sensitive payload", encoding="utf-8")
		audit_path = self.root / ".git/change-governance-failure.jsonl"

		with self.assertRaisesRegex(ChangeGovernanceError, "does not cover"):
			validate_with_audit(
				self.root,
				self.base_commit,
				task_id="COD-008",
				correlation_id="COD-008-audit-error",
				audit_path=audit_path,
			)
		event = json.loads(audit_path.read_text(encoding="utf-8"))
		self.assertEqual(event["status"], "error")
		self.assertEqual(event["error_type"], "ChangeGovernanceError")
		self.assertNotIn("unapproved", json.dumps(event))
		self.assertNotIn("sensitive payload", json.dumps(event))

	def test_invalid_base_and_correlation_id_fail_closed(self) -> None:
		with self.assertRaises(ChangeManagerError):
			resolve_base_commit(self.root, "--unsafe")
		with self.assertRaises(ChangeManagerError):
			resolve_base_commit(self.root, "missing-ref")
		with self.assertRaises(ChangeManagerError):
			normalize_correlation_id("contains a space")

	def test_accepted_adr_body_cannot_be_rewritten(self) -> None:
		self._touch(
			"architecture/adr/ADR-0002-source-controlled-change-governance.md",
			"\n",
		)
		path = self.root / "architecture/adr/ADR-0002-source-controlled-change-governance.md"
		path.write_text(
			path.read_text(encoding="utf-8").replace(
				"工程决策必须在代码合并",
				"工程决策必须始终在代码合并",
				1,
			),
			encoding="utf-8",
		)

		with self.assertRaisesRegex(ChangeGovernanceError, "immutable"):
			build_change_plan(self.root, self.base_commit, task_id="COD-008")

	def test_protected_rename_retains_original_path_for_audit(self) -> None:
		_git(self.root, "mv", "pyproject.toml", "renamed.toml")
		changes = {(row.status, row.path) for row in collect_changed_paths(self.root, self.base_commit)}
		self.assertIn(("D", "pyproject.toml"), changes)
		self.assertIn(("A", "renamed.toml"), changes)


if __name__ == "__main__":
	unittest.main()
