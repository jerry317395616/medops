from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core_build_integrity import MOBILE, ROUTE, IntegrityError, seal, validate_source, verify


class CoreBuildIntegrityTest(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		self.app = self.root / "app"
		self.app.mkdir()
		self.proof = self.root / "build.json"
		self.git("init", "--quiet")
		for name, content in {
			"frontend/src/app.ts": "source",
			"package.json": "{}",
			".gitignore": "*.ignored\n",
			MOBILE + "index.html": "old",
			ROUTE: "old",
			MOBILE + "assets/old-12345678.js": "old",
			MOBILE + "manifest.webmanifest": "manifest",
			MOBILE + "service-worker.js": "worker",
			"frontend/public/manifest.webmanifest": "manifest",
			"frontend/public/service-worker.js": "worker",
		}.items():
			self.write(name, content)
		self.git("add", ".")
		self.git(
			"-c",
			"user.name=Integrity Test",
			"-c",
			"user.email=test@example.invalid",
			"commit",
			"-qm",
			"fixture",
		)
		self.commit = self.git("rev-parse", "HEAD").strip()
		validate_source(self.app, expected_head=self.commit, clean=True)
		(self.app / (MOBILE + "assets/old-12345678.js")).unlink()
		self.write(MOBILE + "assets/new-abcdefgh.js", "built js")
		self.write(MOBILE + "assets/new-abcdefgh.css", "built css")
		self.write(MOBILE + "index.html", "built html")
		self.write(ROUTE, "built html")

	def git(self, *args):
		return subprocess.run(
			["git", "-C", str(self.app), *args], check=True, capture_output=True, text=True
		).stdout

	def write(self, relative, content):
		path = self.app / relative
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(content, encoding="utf-8")

	def test_build_replaces_hashes_but_sources_and_final_inventory_are_verified(self):
		digest = seal(self.app, self.proof, expected_head=self.commit)
		verify(self.app, self.proof, digest, expected_head=self.commit)
		self.assertNotIn(MOBILE + "assets/old-12345678.js", json.loads(self.proof.read_bytes())["files"])
		self.assertNotEqual(self.git("status", "--porcelain"), "")
		with self.assertRaises(IntegrityError):
			validate_source(self.app, expected_head=self.commit, clean=True)
		with self.assertRaises(IntegrityError):
			seal(self.app, self.proof, expected_head=self.commit)

	def test_source_change_untracked_code_index_change_and_wrong_revision_are_rejected(self):
		for path in (
			"frontend/src/app.ts",
			"package.json",
			MOBILE + "extra.py",
			MOBILE + "assets/hidden.ignored",
		):
			with self.subTest(path=path):
				original = (self.app / path).read_bytes() if (self.app / path).exists() else None
				self.write(path, "changed")
				with self.assertRaises(IntegrityError):
					seal(self.app, self.proof, expected_head=self.commit)
				if original is None:
					(self.app / path).unlink()
				else:
					(self.app / path).write_bytes(original)
		with self.assertRaises(IntegrityError):
			seal(self.app, self.proof, expected_head="0" * 40)
		self.git("add", ROUTE)
		with self.assertRaises(IntegrityError):
			seal(self.app, self.proof, expected_head=self.commit)

	def test_any_post_build_change_or_proof_replacement_is_rejected(self):
		digest = seal(self.app, self.proof, expected_head=self.commit)
		for path in (
			MOBILE + "assets/new-abcdefgh.js",
			"frontend/src/app.ts",
			MOBILE + "assets/extra-12345678.css",
		):
			with self.subTest(path=path):
				original = (self.app / path).read_bytes() if (self.app / path).exists() else None
				self.write(path, "changed")
				with self.assertRaises(IntegrityError):
					verify(self.app, self.proof, digest, expected_head=self.commit)
				if original is None:
					(self.app / path).unlink()
				else:
					(self.app / path).write_bytes(original)
		self.proof.write_text("{}", encoding="utf-8")
		with self.assertRaises(IntegrityError):
			verify(self.app, self.proof, digest, expected_head=self.commit)

	def test_generated_route_and_copied_public_files_must_match(self):
		for path in (ROUTE, MOBILE + "service-worker.js", MOBILE + "manifest.webmanifest"):
			with self.subTest(path=path):
				original = (self.app / path).read_bytes()
				self.write(path, "incorrect")
				with self.assertRaises(IntegrityError):
					seal(self.app, self.proof, expected_head=self.commit)
				(self.app / path).write_bytes(original)
		with self.assertRaises(IntegrityError):
			seal(self.app, self.app / "proof.json", expected_head=self.commit)

	def test_symlinks_and_executable_mode_changes_are_rejected(self):
		digest = seal(self.app, self.proof, expected_head=self.commit)
		path = self.app / (MOBILE + "assets/new-abcdefgh.js")
		with patch.object(Path, "is_symlink", lambda candidate: candidate == path):
			with self.assertRaises(IntegrityError):
				verify(self.app, self.proof, digest, expected_head=self.commit)
		if os.name == "nt":
			return  # Linux CI additionally exercises real filesystem symlinks/mode bits.
		original = path.read_bytes()
		path.unlink()
		path.symlink_to(self.app / "frontend/src/app.ts")
		with self.assertRaises(IntegrityError):
			verify(self.app, self.proof, digest, expected_head=self.commit)
		path.unlink()
		path.write_bytes(original)
		path.chmod(0o755)
		with self.assertRaises(IntegrityError):
			verify(self.app, self.proof, digest, expected_head=self.commit)
