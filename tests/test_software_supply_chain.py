from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from medops.common.software_supply_chain import (
	SoftwareSupplyChainContractError,
	compose_cyclonedx_sbom,
	evaluate_security_reports,
	load_software_supply_chain_policy,
	parse_software_supply_chain_policy,
	validate_cyclonedx_sbom,
)
from scripts.security_supply_chain import (
	SecurityExecutionError,
	build_child_environment,
	build_plan,
	normalize_artifact_directory,
	redacted_gitleaks_diagnostics,
	verify_tool_versions,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "d4f1387e73e3a36fe1e16a0b6804e5637383f5ce"
NOW = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)


def _policy_payload() -> dict:
	return json.loads((ROOT / "medops/config/software_supply_chain.json").read_text(encoding="utf-8"))


def _npm_sbom() -> dict:
	root_ref = "medops-quality-tooling@0.1.0"
	component_ref = "eslint@10.8.0"
	return {
		"$schema": "http://cyclonedx.org/schema/bom-1.5.schema.json",
		"bomFormat": "CycloneDX",
		"specVersion": "1.5",
		"serialNumber": "urn:uuid:0a4f2dd5-c911-4afb-a481-4a66a62e24d1",
		"version": 1,
		"metadata": {
			"timestamp": "2026-07-30T00:00:00Z",
			"tools": {"vendor": "npm", "name": "cli", "version": "11.17.0"},
			"component": {
				"bom-ref": root_ref,
				"type": "library",
				"name": "local-folder-name",
				"version": "0.1.0",
				"purl": "pkg:npm/medops-quality-tooling@0.1.0",
				"properties": [{"name": "cdx:npm:package:private", "value": "true"}],
			},
		},
		"components": [
			{
				"bom-ref": component_ref,
				"type": "library",
				"name": "eslint",
				"version": "10.8.0",
				"purl": "pkg:npm/eslint@10.8.0",
				"licenses": [{"license": {"id": "MIT"}}],
				"properties": [{"name": "cdx:npm:package:development", "value": "true"}],
			}
		],
		"dependencies": [
			{"ref": root_ref, "dependsOn": [component_ref]},
			{"ref": component_ref, "dependsOn": []},
		],
	}


def _version_lock() -> dict:
	payload = json.loads((ROOT / "resolved_versions.lock.json").read_text(encoding="utf-8"))
	payload["app_version"] = "0.1.0"
	return payload


def _compose(policy=None) -> dict[str, Any]:
	policy = load_software_supply_chain_policy() if policy is None else policy
	return compose_cyclonedx_sbom(
		_npm_sbom(),
		{"dependencies": []},
		_version_lock(),
		{"name": "medops-quality-tooling"},
		source_commit=SOURCE_COMMIT,
		policy=policy,
	)


def _reports(policy, *, built: str = "2026-07-29T07:08:29Z") -> dict[str, Any]:
	return {
		"bandit_report": {"errors": [], "results": []},
		"gitleaks_report": [],
		"pip_audit_report": {"dependencies": []},
		"npm_audit_report": {
			"auditReportVersion": 2,
			"vulnerabilities": {},
			"metadata": {
				"vulnerabilities": {
					"info": 0,
					"low": 0,
					"moderate": 0,
					"high": 0,
					"critical": 0,
					"total": 0,
				}
			},
		},
		"grype_report": {
			"matches": [],
			"descriptor": {
				"name": "grype",
				"version": policy.tool("grype").version,
				"db": {
					"status": {
						"schemaVersion": "v6.1.9",
						"built": built,
						"valid": True,
					}
				},
			},
		},
	}


class SoftwareSupplyChainPolicyTest(unittest.TestCase):
	def test_current_policy_is_strict_safe_and_pinned(self) -> None:
		policy = load_software_supply_chain_policy()
		self.assertEqual(policy.schema_version, 1)
		self.assertEqual(policy.sbom.spec_version, "1.7")
		self.assertEqual(policy.sbom.required_components, ("medops", "frappe", "erpnext", "hrms"))
		self.assertFalse(policy.execution.http_write_enabled)
		self.assertFalse(policy.execution.site_execution_enabled)
		self.assertFalse(policy.execution.production_execution_enabled)
		self.assertEqual(policy.tool("bandit").version, "1.9.4")
		self.assertEqual(policy.tool("pip_audit").version, "2.10.1")
		self.assertEqual(policy.tool("npm").version, "11.17.0")
		self.assertEqual(policy.tool("gitleaks").version, "8.30.1")
		self.assertEqual(policy.tool("grype").version, "0.116.1")
		self.assertEqual(policy.tool("cyclonedx_cli").version, "0.33.1")
		self.assertEqual(len(policy.exceptions), 1)

	def test_policy_rejects_unknown_keys_unsafe_execution_and_credentials(self) -> None:
		cases = []
		unknown = _policy_payload()
		unknown["unexpected"] = True
		cases.append(unknown)
		production = _policy_payload()
		production["execution"]["production_execution_enabled"] = True
		cases.append(production)
		http_write = _policy_payload()
		http_write["execution"]["http_write_enabled"] = True
		cases.append(http_write)
		credential = _policy_payload()
		credential["execution"]["subprocess_environment_allowlist"].append("GITHUB_TOKEN")
		credential["execution"]["subprocess_environment_allowlist"].sort()
		cases.append(credential)
		for payload in cases:
			with self.subTest(payload=payload), self.assertRaises(SoftwareSupplyChainContractError):
				parse_software_supply_chain_policy(payload)

	def test_binary_tool_digest_and_exception_identity_are_strict(self) -> None:
		bad_digest = _policy_payload()
		bad_digest["tools"]["grype"]["linux_sha256"] = "0" * 63
		with self.assertRaisesRegex(SoftwareSupplyChainContractError, "sha256"):
			parse_software_supply_chain_policy(bad_digest)

		duplicate = _policy_payload()
		duplicate["exceptions"].append(copy.deepcopy(duplicate["exceptions"][0]))
		with self.assertRaisesRegex(SoftwareSupplyChainContractError, "duplicate"):
			parse_software_supply_chain_policy(duplicate)


class SoftwareBillOfMaterialsTest(unittest.TestCase):
	def setUp(self) -> None:
		self.policy = load_software_supply_chain_policy()

	def test_composition_is_deterministic_complete_and_redacted(self) -> None:
		first = _compose(self.policy)
		second = _compose(self.policy)
		self.assertEqual(first, second)
		status = validate_cyclonedx_sbom(first, self.policy)
		self.assertEqual(status["component_count"], 6)
		self.assertEqual(status["spec_version"], "1.7")
		self.assertEqual(first["metadata"]["component"]["name"], "medops")
		components = {component["name"]: component for component in first["components"]}
		self.assertEqual(set(("medops", "frappe", "erpnext", "hrms")) - set(components), {"medops"})
		self.assertEqual(components["medops-quality-tooling"]["scope"], "excluded")
		self.assertEqual(components["eslint"]["scope"], "excluded")
		self.assertIn("pkg:github/jerry317395616/frappe@", components["frappe"]["purl"])
		serialized = json.dumps(first, ensure_ascii=False)
		self.assertNotIn("local-folder-name", serialized)
		self.assertNotRegex(serialized, r"[A-Za-z]:\\\\")
		self.assertNotIn("/home/", serialized)
		self.assertNotIn("timestamp", first["metadata"])

	def test_composition_rejects_invalid_source_or_version_lock(self) -> None:
		with self.assertRaisesRegex(SoftwareSupplyChainContractError, "full Git SHA"):
			compose_cyclonedx_sbom(
				_npm_sbom(),
				{"dependencies": []},
				_version_lock(),
				{"name": "medops-quality-tooling"},
				source_commit="main",
				policy=self.policy,
			)
		bad_lock = _version_lock()
		bad_lock["apps"]["frappe"]["repository"] = "https://example.invalid/frappe.git"
		with self.assertRaisesRegex(SoftwareSupplyChainContractError, "source is invalid"):
			compose_cyclonedx_sbom(
				_npm_sbom(),
				{"dependencies": []},
				bad_lock,
				{"name": "medops-quality-tooling"},
				source_commit=SOURCE_COMMIT,
				policy=self.policy,
			)

	def test_validator_rejects_local_paths_duplicate_refs_and_unknown_edges(self) -> None:
		local_path = _compose(self.policy)
		local_path["components"][0]["description"] = r"C:\Users\secret\package"
		with self.assertRaisesRegex(SoftwareSupplyChainContractError, "local filesystem"):
			validate_cyclonedx_sbom(local_path, self.policy)

		duplicate = _compose(self.policy)
		duplicate["components"].append(copy.deepcopy(duplicate["components"][0]))
		with self.assertRaisesRegex(SoftwareSupplyChainContractError, "duplicate"):
			validate_cyclonedx_sbom(duplicate, self.policy)

		unknown = _compose(self.policy)
		unknown["dependencies"][0]["dependsOn"].append("pkg:npm/not-present@1.0.0")
		with self.assertRaisesRegex(SoftwareSupplyChainContractError, "unknown component"):
			validate_cyclonedx_sbom(unknown, self.policy)


class SecurityReportEvaluationTest(unittest.TestCase):
	def setUp(self) -> None:
		self.policy = load_software_supply_chain_policy()
		self.sbom = _compose(self.policy)

	def evaluate(self, **overrides: object) -> dict[str, Any]:
		reports = _reports(self.policy)
		reports.update(overrides)
		return cast(
			dict[str, Any],
			evaluate_security_reports(
				policy=self.policy,
				sbom=self.sbom,
				source_commit=SOURCE_COMMIT,
				now=NOW,
				**reports,
			),
		)

	def test_clean_reports_pass_with_fresh_database(self) -> None:
		result = self.evaluate()
		self.assertTrue(result["passed"])
		self.assertEqual(result["status"], "pass")
		self.assertEqual(result["violations"], [])
		self.assertEqual(result["findings"]["grype_blocking"], 0)
		self.assertTrue(result["vulnerability_database"]["valid"])

	def test_each_security_gate_fails_closed(self) -> None:
		cases = {
			"bandit_findings": {
				"bandit_report": {
					"errors": [],
					"results": [
						{
							"issue_severity": "HIGH",
							"issue_confidence": "HIGH",
							"test_id": "B999",
							"filename": "medops/example.py",
						}
					],
				}
			},
			"secret_findings": {"gitleaks_report": [{"RuleID": "generic-api-key"}]},
			"pip_audit_vulnerabilities": {
				"pip_audit_report": {
					"dependencies": [{"name": "unsafe", "version": "1.0.0", "vulns": [{"id": "PYSEC-1"}]}]
				}
			},
			"npm_audit_vulnerabilities": {
				"npm_audit_report": {
					"auditReportVersion": 2,
					"vulnerabilities": {"unsafe": {}},
					"metadata": {
						"vulnerabilities": {
							"info": 0,
							"low": 0,
							"moderate": 0,
							"high": 1,
							"critical": 0,
							"total": 1,
						}
					},
				}
			},
			"grype_vulnerabilities": {
				"grype_report": {
					"matches": [
						{
							"artifact": {"name": "unsafe"},
							"vulnerability": {"id": "CVE-2099-0001", "severity": "Critical"},
						}
					],
					"descriptor": _reports(self.policy)["grype_report"]["descriptor"],
				}
			},
		}
		for expected, override in cases.items():
			with self.subTest(expected=expected):
				result = self.evaluate(**override)
				self.assertFalse(result["passed"])
				self.assertIn(expected, result["violations"])

	def test_database_and_license_gates_fail_closed(self) -> None:
		stale = _reports(self.policy, built="2026-07-01T00:00:00Z")["grype_report"]
		self.assertIn(
			"grype_database_stale",
			self.evaluate(grype_report=stale)["violations"],
		)

		invalid = _reports(self.policy)["grype_report"]
		invalid["descriptor"]["db"]["status"]["valid"] = False
		self.assertIn(
			"grype_database_invalid",
			self.evaluate(grype_report=invalid)["violations"],
		)

		sbom = copy.deepcopy(self.sbom)
		sbom["components"][0]["licenses"] = [{"license": {"id": "BUSL-1.1"}}]
		reports = _reports(self.policy)
		result = cast(
			dict[str, Any],
			evaluate_security_reports(
				policy=self.policy,
				sbom=sbom,
				source_commit=SOURCE_COMMIT,
				now=NOW,
				**reports,
			),
		)
		self.assertIn("denied_licenses", result["violations"])

	def test_expired_exception_fails_even_without_matching_finding(self) -> None:
		payload = _policy_payload()
		payload["exceptions"][0]["expires_on"] = "2026-07-01"
		policy = parse_software_supply_chain_policy(payload)
		result = cast(
			dict[str, Any],
			evaluate_security_reports(
				policy=policy,
				sbom=_compose(policy),
				source_commit=SOURCE_COMMIT,
				now=NOW,
				**_reports(policy),
			),
		)
		self.assertIn("expired_exceptions", result["violations"])


class SecurityRunnerBoundaryTest(unittest.TestCase):
	def setUp(self) -> None:
		self.policy = load_software_supply_chain_policy()

	def test_artifact_path_is_confined_and_plan_is_read_only(self) -> None:
		governed = normalize_artifact_directory(None, root=ROOT, policy=self.policy)
		self.assertEqual(governed, (ROOT / ".artifacts/security").resolve())
		with tempfile.TemporaryDirectory() as temp:
			with self.assertRaisesRegex(SecurityExecutionError, "must remain under"):
				normalize_artifact_directory(temp, root=ROOT, policy=self.policy)
		plan = build_plan(self.policy, artifact_directory=governed, source_commit=SOURCE_COMMIT)
		self.assertFalse(plan["site_execution_enabled"])
		self.assertFalse(plan["production_execution_enabled"])
		self.assertFalse(plan["raw_reports_uploaded"])

	def test_child_environment_is_allowlisted_and_credentials_are_removed(self) -> None:
		child = build_child_environment(
			self.policy,
			{
				"PATH": "safe-path",
				"HOME": "safe-home",
				"GITHUB_TOKEN": "secret",
				"NPM_TOKEN": "secret",
				"PIP_INDEX_URL": "https://user:secret@example.invalid",
				"GRYPE_DB_UPDATE_URL": "https://evil.invalid",
			},
		)
		self.assertEqual(child["PATH"], "safe-path")
		self.assertEqual(child["HOME"], "safe-home")
		self.assertNotIn("GITHUB_TOKEN", child)
		self.assertNotIn("NPM_TOKEN", child)
		self.assertNotIn("PIP_INDEX_URL", child)
		self.assertNotIn("GRYPE_DB_UPDATE_URL", child)
		self.assertEqual(child["GRYPE_CHECK_FOR_APP_UPDATE"], "false")

	def test_gitleaks_failure_diagnostics_are_bounded_and_redacted(self) -> None:
		diagnostics = redacted_gitleaks_diagnostics(
			[
				{
					"RuleID": "generic-api-key",
					"File": "tests/example file.py",
					"StartLine": 27,
					"Commit": SOURCE_COMMIT,
					"Secret": "must-not-appear",
					"Match": "api_key=must-not-appear",
				}
			]
		)
		self.assertEqual(
			diagnostics,
			(
				"GITLEAKS FINDING: rule=generic-api-key file=tests/example?file.py "
				"line=27 commit=d4f1387e73e3",
			),
		)
		self.assertNotIn("must-not-appear", diagnostics[0])

	def test_tool_versions_are_verified_exactly(self) -> None:
		outputs = (
			"bandit 1.9.4",
			"pip-audit 2.10.1",
			"11.17.0",
			"8.30.1",
			"Application: grype\nVersion: 0.116.1",
			"0.33.1+commit",
		)
		results = [
			subprocess.CompletedProcess(args=(), returncode=0, stdout=output, stderr="") for output in outputs
		]
		with patch("scripts.security_supply_chain._run", side_effect=results):
			versions = verify_tool_versions(
				policy=self.policy,
				npm_bin="npm",
				gitleaks_bin="gitleaks",
				grype_bin="grype",
				cyclonedx_bin="cyclonedx",
				environment={},
			)
		self.assertEqual(versions["grype"], "0.116.1")

		results[0] = subprocess.CompletedProcess(
			args=(),
			returncode=0,
			stdout="bandit 9.9.9",
			stderr="",
		)
		with (
			patch("scripts.security_supply_chain._run", side_effect=results),
			self.assertRaisesRegex(SecurityExecutionError, "bandit 1.9.4 is required"),
		):
			verify_tool_versions(
				policy=self.policy,
				npm_bin="npm",
				gitleaks_bin="gitleaks",
				grype_bin="grype",
				cyclonedx_bin="cyclonedx",
				environment={},
			)


if __name__ == "__main__":
	unittest.main()
