from __future__ import annotations

import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.version_lock import (
	DEFAULT_LOCK,
	EXPECTED_REPOSITORIES,
	UPSTREAM_APPS,
	VersionLockError,
	compare_bench,
	load_lock,
	normalize_repository,
	validate_lock,
)


class VersionLockTest(unittest.TestCase):
	def test_repository_lock_is_valid_and_complete(self) -> None:
		lock = load_lock(DEFAULT_LOCK)
		self.assertEqual(tuple(lock["apps"]), UPSTREAM_APPS)
		self.assertEqual(validate_lock(lock), [])

	def test_rejects_placeholder_or_unofficial_source(self) -> None:
		lock = deepcopy(load_lock(DEFAULT_LOCK))
		lock["apps"]["frappe"]["commit"] = "RESOLVE_AT_BOOTSTRAP"
		lock["apps"]["hrms"]["repository"] = "https://example.com/hrms.git"
		issues = validate_lock(lock)
		self.assertIn(
			"frappe commit must be a lowercase 40-character SHA",
			issues,
		)
		self.assertIn(
			"hrms repository must be https://github.com/jerry317395616/hrms.git",
			issues,
		)

	def test_normalizes_supported_github_remote_forms(self) -> None:
		self.assertEqual(
			normalize_repository("git@github.com:jerry317395616/frappe.git"),
			normalize_repository("https://github.com/jerry317395616/frappe"),
		)

	def test_rejects_direct_official_and_other_owner_sources(self) -> None:
		for owner in ("frappe", "another-owner"):
			for app in UPSTREAM_APPS:
				with self.subTest(owner=owner, app=app):
					lock = deepcopy(load_lock(DEFAULT_LOCK))
					lock["apps"][app]["repository"] = f"https://github.com/{owner}/{app}.git"
					self.assertTrue(any("repository must be" in issue for issue in validate_lock(lock)))

	def test_accepts_bench_upstream_remote_name(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			bench = Path(temp)
			lock = {"schema_version": 1, "apps": {}}
			for app in UPSTREAM_APPS:
				app_root = bench / "apps" / app
				package_root = app_root / app
				package_root.mkdir(parents=True)
				(package_root / "__init__.py").write_text(
					'__version__ = "17.0.0-dev"\n',
					encoding="utf-8",
				)
				self._git(app_root, "init")
				self._git(app_root, "config", "user.email", "test@example.com")
				self._git(app_root, "config", "user.name", "Version Lock Test")
				self._git(app_root, "add", ".")
				self._git(app_root, "commit", "-m", "fixture")
				self._git(
					app_root,
					"remote",
					"add",
					"upstream",
					EXPECTED_REPOSITORIES[app],
				)
				lock["apps"][app] = {
					"repository": EXPECTED_REPOSITORIES[app],
					"branch": "develop",
					"commit": self._git(app_root, "rev-parse", "HEAD"),
					"version": "17.0.0-dev",
				}
			report = compare_bench(lock, bench)
			self.assertEqual(report["status"], "ok", report["issues"])
			self.assertEqual(report["apps"]["frappe"]["remote_name"], "upstream")

	def test_invalid_json_raises_version_lock_error(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			path = Path(temp) / "lock.json"
			path.write_text("{", encoding="utf-8")
			with self.assertRaisesRegex(VersionLockError, "invalid version lock JSON"):
				load_lock(path)

	def test_bench_verification_detects_commit_and_dirty_mismatches(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			bench = Path(temp)
			lock = {
				"schema_version": 1,
				"apps": {},
			}
			for app in UPSTREAM_APPS:
				app_root = bench / "apps" / app
				package_root = app_root / app
				package_root.mkdir(parents=True)
				(package_root / "__init__.py").write_text(
					'__version__ = "17.0.0-dev"\n',
					encoding="utf-8",
				)
				self._git(app_root, "init")
				self._git(app_root, "config", "user.email", "test@example.com")
				self._git(app_root, "config", "user.name", "Version Lock Test")
				self._git(app_root, "add", ".")
				self._git(app_root, "commit", "-m", "fixture")
				self._git(app_root, "remote", "add", "origin", EXPECTED_REPOSITORIES[app])
				lock["apps"][app] = {
					"repository": EXPECTED_REPOSITORIES[app],
					"branch": "develop",
					"commit": self._git(app_root, "rev-parse", "HEAD"),
					"version": "17.0.0-dev",
				}

			clean = compare_bench(lock, bench)
			self.assertEqual(clean["status"], "ok")
			self.assertEqual(clean["issues"], [])

			lock["apps"]["erpnext"]["commit"] = "0" * 40
			(bench / "apps" / "hrms" / "untracked.txt").write_text(
				"dirty",
				encoding="utf-8",
			)
			mismatch = compare_bench(lock, bench)
			self.assertEqual(mismatch["status"], "mismatch")
			self.assertTrue(any(issue.startswith("erpnext: commit mismatch") for issue in mismatch["issues"]))
			self.assertIn("hrms: worktree is dirty", mismatch["issues"])

	@staticmethod
	def _git(root: Path, *args: str) -> str:
		result = subprocess.run(
			["git", *args],
			cwd=root,
			text=True,
			capture_output=True,
			check=True,
		)
		return result.stdout.strip()


if __name__ == "__main__":
	unittest.main()
