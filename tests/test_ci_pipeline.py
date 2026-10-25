from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.repository_contract import validate_ci_pipeline

ROOT = Path(__file__).resolve().parents[1]
CI_FILES = (
	".github/workflows/ci.yml",
	"scripts/bootstrap_latest_develop.sh",
	"scripts/ci_integration.sh",
)


def copy_ci_files(target_root: Path) -> None:
	for relative_path in CI_FILES:
		target = target_root / relative_path
		target.parent.mkdir(parents=True, exist_ok=True)
		shutil.copyfile(ROOT / relative_path, target)


class CIPipelineTest(unittest.TestCase):
	def test_current_pipeline_satisfies_contract(self) -> None:
		self.assertEqual(validate_ci_pipeline(ROOT), [])

	def test_rejects_floating_action_reference(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			copy_ci_files(root)
			workflow_path = root / ".github/workflows/ci.yml"
			workflow = workflow_path.read_text(encoding="utf-8").replace(
				"actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
				"actions/checkout@v6",
				1,
			)
			workflow_path.write_text(workflow, encoding="utf-8")
			self.assertTrue(
				any("pin a full commit SHA" in violation for violation in validate_ci_pipeline(root))
			)

	def test_rejects_privileged_pull_request_target(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			copy_ci_files(root)
			workflow_path = root / ".github/workflows/ci.yml"
			workflow = workflow_path.read_text(encoding="utf-8").replace(
				"  pull_request:\n",
				"  pull_request_target:\n",
				1,
			)
			workflow_path.write_text(workflow, encoding="utf-8")
			violations = validate_ci_pipeline(root)
			self.assertIn("CI must not use pull_request_target", violations)

	def test_rejects_non_aggregating_required_job(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			copy_ci_files(root)
			workflow_path = root / ".github/workflows/ci.yml"
			workflow = workflow_path.read_text(encoding="utf-8").replace(
				"  required:\n    name: Required\n    if: always()\n",
				"  required:\n    name: Required\n    if: success()\n",
				1,
			)
			workflow_path.write_text(workflow, encoding="utf-8")
			self.assertIn(
				"CI required job must run with always()",
				validate_ci_pipeline(root),
			)

	def test_rejects_required_job_without_security(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			copy_ci_files(root)
			workflow_path = root / ".github/workflows/ci.yml"
			workflow = workflow_path.read_text(encoding="utf-8").replace(
				"      - security\n",
				"",
				1,
			)
			workflow_path.write_text(workflow, encoding="utf-8")
			self.assertIn(
				"CI required job must aggregate quality, security and integration",
				validate_ci_pipeline(root),
			)

	def test_rejects_missing_secret_scan(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			copy_ci_files(root)
			workflow_path = root / ".github/workflows/ci.yml"
			workflow = workflow_path.read_text(encoding="utf-8").replace(
				"gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz",
				"gitleaks-asset-removed.tar.gz",
			)
			workflow_path.write_text(workflow, encoding="utf-8")
			self.assertIn(
				"CI security job is missing command: gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz",
				validate_ci_pipeline(root),
			)

	def test_rejects_drifted_security_binary_digest(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			copy_ci_files(root)
			workflow_path = root / ".github/workflows/ci.yml"
			workflow = workflow_path.read_text(encoding="utf-8").replace(
				"551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
				"0" * 64,
				1,
			)
			workflow_path.write_text(workflow, encoding="utf-8")
			self.assertIn(
				"CI must pin GITLEAKS_LINUX_X64_SHA256 to the governed security policy",
				validate_ci_pipeline(root),
			)

	def test_rejects_integration_without_locked_version_check(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			copy_ci_files(root)
			script_path = root / "scripts/ci_integration.sh"
			script = script_path.read_text(encoding="utf-8").replace(
				'"$ROOT_DIR/scripts/version_lock.py"',
				'"$ROOT_DIR/scripts/version_check_removed.py"',
				1,
			)
			script_path.write_text(script, encoding="utf-8")
			self.assertIn(
				"CI integration script is missing: version_lock.py",
				validate_ci_pipeline(root),
			)

	def test_rejects_bootstrap_without_clean_pinned_upstream_worktrees(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			copy_ci_files(root)
			script_path = root / "scripts/bootstrap_latest_develop.sh"
			script = script_path.read_text(encoding="utf-8").replace(
				'  git -C "$app_dir" clean -fd\n',
				"",
				1,
			)
			script_path.write_text(script, encoding="utf-8")
			self.assertIn(
				'locked Bench bootstrap is missing: git -C "$app_dir" clean -fd',
				validate_ci_pipeline(root),
			)

	def test_rejects_quality_job_without_change_governance(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			copy_ci_files(root)
			workflow_path = root / ".github/workflows/ci.yml"
			workflow = workflow_path.read_text(encoding="utf-8").replace(
				"scripts/change_manager.py",
				"scripts/change_manager_removed.py",
			)
			workflow_path.write_text(workflow, encoding="utf-8")
			self.assertIn(
				"CI quality job is missing command: python scripts/change_manager.py",
				validate_ci_pipeline(root),
			)

	def test_rejects_quality_job_without_pinned_k6_inspection(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			copy_ci_files(root)
			workflow_path = root / ".github" / "workflows" / "ci.yml"
			workflow = workflow_path.read_text(encoding="utf-8").replace(
				"sha256sum --check",
				"checksum-validation-removed",
				1,
			)
			workflow_path.write_text(workflow, encoding="utf-8")
			self.assertIn(
				"CI quality job is missing command: sha256sum --check",
				validate_ci_pipeline(root),
			)
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			copy_ci_files(root)
			workflow_path = root / ".github" / "workflows" / "ci.yml"
			workflow = workflow_path.read_text(encoding="utf-8").replace(
				"medops/load_tests/performance_baseline.js",
				"performance-script-inspection-removed",
				1,
			)
			workflow_path.write_text(workflow, encoding="utf-8")
			self.assertIn(
				"CI quality job is missing command: medops/load_tests/performance_baseline.js",
				validate_ci_pipeline(root),
			)


if __name__ == "__main__":
	unittest.main()
