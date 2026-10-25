from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from medops.common.performance_baseline import (
	PERFORMANCE_BASELINES_PATH,
	PerformanceBaselineContractError,
	evaluate_performance_run,
	load_performance_baseline_registry,
	parse_performance_baseline_registry,
	parse_performance_run_summary,
)
from scripts.checksums import calculate
from scripts.performance_baseline import (
	build_child_environment,
	build_plan,
	execute,
	normalize_base_url,
	normalize_output_path,
)

ROOT = Path(__file__).resolve().parents[1]


class TestPerformanceBaselineContract(unittest.TestCase):
	def _payload(self) -> dict[str, Any]:
		return json.loads(PERFORMANCE_BASELINES_PATH.read_text(encoding="utf-8"))

	def _summary(self) -> dict[str, Any]:
		registry = load_performance_baseline_registry()
		return {
			"schema_version": 1,
			"scenario_id": "platform-module-registry-read",
			"scenario_version": 1,
			"profile": "baseline",
			"registry_sha256": registry.sha256,
			"tool_version": "2.1.0",
			"run_id": "perf-COD-015-unit-0001",
			"metrics": {
				"request_count": 200,
				"error_rate": 0,
				"check_rate": 1,
				"requests_per_second": 20,
				"p95_ms": 100,
				"p99_ms": 200,
				"duration_ms": 10_000,
			},
		}

	def test_current_registry_is_deterministic_bounded_and_read_only(self) -> None:
		first = load_performance_baseline_registry()
		second = load_performance_baseline_registry()
		self.assertEqual(first.sha256, second.sha256)
		self.assertEqual(first.schema_version, 1)
		self.assertEqual(first.app, "medops")
		self.assertEqual(first.policy.k6_version, "2.1.0")
		self.assertEqual(len(first.scenarios), 1)
		scenario = first.get("platform-module-registry-read")
		self.assertEqual(scenario.method, "GET")
		self.assertTrue(scenario.read_only)
		self.assertFalse(scenario.contains_personal_data)
		self.assertEqual(
			[profile.profile for profile in scenario.profiles],
			["smoke", "baseline", "load"],
		)
		for profile in scenario.profiles:
			self.assertLessEqual(profile.virtual_users, first.policy.max_virtual_users)
			self.assertLessEqual(profile.iterations, first.policy.max_iterations)
			self.assertLessEqual(profile.max_duration_seconds, first.policy.max_duration_seconds)

	def test_public_contract_exposes_limits_without_write_or_credentials(self) -> None:
		contract = cast(dict[str, Any], load_performance_baseline_registry().as_public_dict())
		self.assertEqual(contract["scenario_count"], 1)
		self.assertFalse(contract["execution_policy"]["http_write_enabled"])
		self.assertEqual(
			contract["execution_policy"]["execution_location"],
			"external_k6_process",
		)
		self.assertEqual(
			contract["execution_policy"]["idempotency"],
			"read_only_replay_safe",
		)
		self.assertFalse(contract["result_policy"]["raw_credentials_persisted"])
		self.assertFalse(contract["result_policy"]["target_url_persisted"])
		serialized = json.dumps(contract, ensure_ascii=False)
		self.assertNotIn("api_secret", serialized)
		self.assertNotIn("Authorization", serialized)

	def test_rejects_writes_personal_data_and_unsafe_paths(self) -> None:
		for field, value, message in (
			("method", "POST", "must be GET"),
			("contains_personal_data", True, "must be false"),
			("read_only", False, "must be true"),
			("path", "/api/method/medops.api.v1.modules.list_modules?debug=1", "unsafe"),
		):
			payload = self._payload()
			payload["scenarios"][0][field] = value
			with (
				self.subTest(field=field),
				self.assertRaisesRegex(
					PerformanceBaselineContractError,
					message,
				),
			):
				parse_performance_baseline_registry(payload)

	def test_rejects_production_and_resource_limit_bypass(self) -> None:
		payload = self._payload()
		payload["policy"]["allowed_profiles"].append("production")
		with self.assertRaisesRegex(PerformanceBaselineContractError, "unsupported"):
			parse_performance_baseline_registry(payload)

		payload = self._payload()
		payload["scenarios"][0]["profiles"][2]["virtual_users"] = 51
		with self.assertRaisesRegex(PerformanceBaselineContractError, "virtual_users"):
			parse_performance_baseline_registry(payload)

		payload = self._payload()
		payload["policy"]["max_iterations"] = True
		with self.assertRaisesRegex(PerformanceBaselineContractError, "integer"):
			parse_performance_baseline_registry(payload)

	def test_rejects_invalid_thresholds_and_unbounded_rate_claim(self) -> None:
		payload = self._payload()
		payload["scenarios"][0]["profiles"][1]["thresholds"]["p95_ms"] = 2000
		with self.assertRaisesRegex(PerformanceBaselineContractError, "must not exceed"):
			parse_performance_baseline_registry(payload)

		payload = self._payload()
		payload["scenarios"][0]["profiles"][1]["thresholds"]["min_requests_per_second"] = 100
		with self.assertRaisesRegex(PerformanceBaselineContractError, "bounded run rate"):
			parse_performance_baseline_registry(payload)

	def test_passing_summary_is_independently_evaluated(self) -> None:
		registry = load_performance_baseline_registry()
		summary = parse_performance_run_summary(self._summary())
		evaluation = evaluate_performance_run(summary, registry)
		self.assertTrue(evaluation["passed"])
		self.assertEqual(evaluation["status"], "pass")
		self.assertEqual(evaluation["violations"], [])
		thresholds = cast(dict[str, object], evaluation["thresholds"])
		self.assertEqual(thresholds["p95_ms"], 750)

	def test_result_evaluator_reports_identity_and_metric_failures(self) -> None:
		registry = load_performance_baseline_registry()
		payload = self._summary()
		payload["registry_sha256"] = "0" * 64
		payload["tool_version"] = "1.7.1"
		payload["scenario_version"] = 2
		payload["metrics"].update(
			{
				"request_count": 199,
				"error_rate": 0.02,
				"check_rate": 0.98,
				"requests_per_second": 2,
				"p95_ms": 751,
				"p99_ms": 1501,
			}
		)
		evaluation = evaluate_performance_run(parse_performance_run_summary(payload), registry)
		self.assertFalse(evaluation["passed"])
		violations = cast(list[str], evaluation["violations"])
		self.assertEqual(
			set(violations),
			{
				"registry_sha256_mismatch",
				"tool_version_mismatch",
				"scenario_version_mismatch",
				"request_count",
				"error_rate",
				"check_rate",
				"requests_per_second",
				"p95_ms",
				"p99_ms",
			},
		)

	def test_summary_parser_is_strict_and_rejects_boolean_metrics(self) -> None:
		payload = self._summary()
		payload["metrics"]["request_count"] = True
		with self.assertRaisesRegex(PerformanceBaselineContractError, "integer"):
			parse_performance_run_summary(payload)

		payload = self._summary()
		payload["target_url"] = "https://secret.example"
		with self.assertRaisesRegex(PerformanceBaselineContractError, "keys mismatch"):
			parse_performance_run_summary(payload)

	def test_runner_plan_redacts_host_and_rejects_unsafe_targets(self) -> None:
		base_url = normalize_base_url("https://hrp-test.example.com/")
		self.assertEqual(base_url, "https://hrp-test.example.com")
		plan = build_plan(base_url, "platform-module-registry-read", "smoke")
		self.assertEqual(plan["target"], {"scheme": "https", "loopback": False})
		self.assertNotIn("hrp-test.example.com", json.dumps(plan))
		self.assertFalse(plan["persists_credentials"])
		self.assertFalse(plan["persists_target_url"])
		self.assertEqual(normalize_base_url("http://127.0.0.1:8200"), "http://127.0.0.1:8200")
		for target in (
			"http://production.example.com",
			"https://account:" + "credential@example.com",
			"https://example.com/path",
			"https://example.com?token=secret",
		):
			with self.subTest(target=target), self.assertRaises(ValueError):
				normalize_base_url(target)

	def test_runner_confines_reports_and_sanitizes_child_environment(self) -> None:
		output_path = normalize_output_path(".artifacts/performance/smoke.json")
		self.assertEqual(output_path.name, "smoke.json")
		for unsafe in (
			"performance.json",
			".artifacts/performance/../../VALIDATION_REPORT.json",
			".artifacts/performance/result.txt",
			"C:/temporary/performance.json",
		):
			with self.subTest(unsafe=unsafe), self.assertRaisesRegex(ValueError, "inside"):
				normalize_output_path(unsafe)
		child_environment = build_child_environment(
			{
				"Path": "safe-path",
				"HTTPS_PROXY": "https://proxy.example",
				"K6_HTTP_DEBUG": "full",
				"K6_INSECURE_SKIP_TLS_VERIFY": "true",
				"K6_OUT": "cloud",
				"UNRELATED_SECRET": "must-not-pass",
			},
			{"IONE_PERF_RUN_ID": "perf-COD-015-unit-0002"},
		)
		self.assertEqual(child_environment["Path"], "safe-path")
		self.assertEqual(child_environment["HTTPS_PROXY"], "https://proxy.example")
		self.assertEqual(child_environment["IONE_PERF_RUN_ID"], "perf-COD-015-unit-0002")
		self.assertNotIn("K6_HTTP_DEBUG", child_environment)
		self.assertNotIn("K6_INSECURE_SKIP_TLS_VERIFY", child_environment)
		self.assertNotIn("K6_OUT", child_environment)
		self.assertNotIn("UNRELATED_SECRET", child_environment)
		runner_source = (ROOT / "scripts" / "performance_baseline.py").read_text(encoding="utf-8")
		self.assertIn('"IONE_PERF_CONFIRM": CONFIRMATION', runner_source)

	def test_generated_performance_artifacts_are_excluded_from_repository_checksums(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			artifact = root / ".artifacts" / "performance" / "smoke.json"
			artifact.parent.mkdir(parents=True)
			artifact.write_text('{"status":"pass"}\n', encoding="utf-8")
			(root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
			self.assertEqual(set(calculate(root)), {"tracked.txt"})

	def test_runner_rejects_unpinned_k6_before_starting_a_load_test(self) -> None:
		args = Namespace(
			base_url="http://127.0.0.1:8200",
			scenario="platform-module-registry-read",
			profile="smoke",
			output=".artifacts/performance/unit.json",
			k6_binary="k6",
			dry_run=False,
		)
		environment = {
			"IONE_PERF_CONFIRM": "NON_PRODUCTION_LOAD_TEST",
			"IONE_PERF_API_KEY": "placeholder-key",
			"IONE_PERF_API_SECRET": "placeholder-value",
		}
		with (
			patch.dict(os.environ, environment, clear=False),
			patch("scripts.performance_baseline.shutil.which", return_value="k6"),
			patch("scripts.performance_baseline.subprocess.run") as run,
			self.assertRaisesRegex(ValueError, "k6 2.1.0 is required"),
		):
			run.return_value.returncode = 0
			run.return_value.stdout = "k6 v1.7.1"
			run.return_value.stderr = ""
			execute(args)
		run.assert_called_once()

	def test_k6_script_uses_contract_gate_and_source_controlled_get_only_path(self) -> None:
		script = (ROOT / "medops" / "load_tests" / "performance_baseline.js").read_text(encoding="utf-8")
		self.assertIn("get_performance_baseline_contract", script)
		self.assertIn("NON_PRODUCTION_LOAD_TEST", script)
		self.assertIn("load_test_available === true", script)
		self.assertIn('scenario.method !== "GET"', script)
		self.assertIn("shared-iterations", script)
		self.assertIn('"p(99)"', script)
		self.assertIn("summaryTrendStats", script)
		self.assertNotIn("http.post", script)
		self.assertNotIn("insecureSkipTLSVerify", script)
		self.assertIn('"IONE_PERF_API_SECRET"', script)
		self.assertIn("requiredEnvironment(", script)
		self.assertNotIn("--api-secret", script)

	def test_duplicate_scenario_and_profile_order_are_rejected(self) -> None:
		payload = self._payload()
		payload["scenarios"].append(copy.deepcopy(payload["scenarios"][0]))
		with self.assertRaisesRegex(PerformanceBaselineContractError, "duplicate scenario"):
			parse_performance_baseline_registry(payload)

		payload = self._payload()
		payload["scenarios"][0]["profiles"].reverse()
		with self.assertRaisesRegex(PerformanceBaselineContractError, "ordered"):
			parse_performance_baseline_registry(payload)


if __name__ == "__main__":
	unittest.main()
