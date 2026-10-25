from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from medops.common.test_data_factory import (
	TEST_DATA_SCENARIOS_PATH,
	TestDataFactoryContractError,
	load_test_data_scenario_registry,
	normalize_test_data_seed,
	parse_test_data_scenario_registry,
	synthetic_identifier,
	test_dataset_id,
)


class TestTestDataFactoryContract(unittest.TestCase):
	def _payload(self) -> dict[str, Any]:
		return json.loads(TEST_DATA_SCENARIOS_PATH.read_text(encoding="utf-8"))

	def test_current_registry_is_deterministic_and_dependency_ordered(self) -> None:
		first = load_test_data_scenario_registry()
		second = load_test_data_scenario_registry()
		self.assertEqual(first.sha256, second.sha256)
		self.assertEqual(first.schema_version, 1)
		self.assertEqual(first.app, "medops")
		self.assertEqual(len(first.scenarios), 1)
		scenario = first.get("platform-smoke")
		self.assertEqual(
			[step.step_id for step in scenario.ordered_steps()],
			["foundation-marker", "dependency-marker"],
		)
		self.assertEqual(scenario.allowed_profiles, ("development", "test"))

	def test_public_contract_disables_arbitrary_and_http_writes(self) -> None:
		contract = cast(dict[str, Any], load_test_data_scenario_registry().as_public_dict())
		self.assertEqual(contract["scenario_count"], 1)
		policy = contract["generation_policy"]
		self.assertFalse(policy["arbitrary_doctype_input"])
		self.assertFalse(policy["contains_personal_data"])
		self.assertFalse(policy["http_write_enabled"])
		self.assertEqual(policy["cleanup"], "replaceable_environment_reset")
		self.assertNotIn("builder", str(contract["scenarios"]))

	def test_seed_and_identifiers_are_deterministic_without_exposing_seed(self) -> None:
		seed = normalize_test_data_seed("COD-014-seed-0001")
		first_dataset = test_dataset_id("platform-smoke", 1, seed)
		second_dataset = test_dataset_id("platform-smoke", 1, seed)
		other_dataset = test_dataset_id("platform-smoke", 1, "COD-014-seed-0002")
		self.assertEqual(first_dataset, second_dataset)
		self.assertNotEqual(first_dataset, other_dataset)
		self.assertNotIn(seed, first_dataset)
		first_identifier = synthetic_identifier(
			"ione.testdata",
			scenario_id="platform-smoke",
			version=1,
			seed=seed,
			step_id="foundation-marker",
		)
		second_identifier = synthetic_identifier(
			"ione.testdata",
			scenario_id="platform-smoke",
			version=1,
			seed=seed,
			step_id="foundation-marker",
		)
		self.assertEqual(first_identifier, second_identifier)
		self.assertTrue(first_identifier.startswith("ione.testdata."))
		self.assertNotIn(seed, first_identifier)

	def test_rejects_short_or_unsafe_seed(self) -> None:
		for seed in ("short", " leading-seed", "contains space", "含中文的测试种子"):
			with self.subTest(seed=seed), self.assertRaises(TestDataFactoryContractError):
				normalize_test_data_seed(seed)

	def test_rejects_personal_data_and_production_profiles(self) -> None:
		payload = self._payload()
		payload["scenarios"][0]["contains_personal_data"] = True
		with self.assertRaisesRegex(TestDataFactoryContractError, "forbid personal data"):
			parse_test_data_scenario_registry(payload)

		payload = self._payload()
		payload["schema_version"] = True
		with self.assertRaisesRegex(TestDataFactoryContractError, "schema version"):
			parse_test_data_scenario_registry(payload)

		payload = self._payload()
		payload["scenarios"][0]["allowed_profiles"].append("production")
		with self.assertRaisesRegex(TestDataFactoryContractError, "unsafe"):
			parse_test_data_scenario_registry(payload)

	def test_rejects_unknown_builder_and_dependency(self) -> None:
		payload = self._payload()
		payload["scenarios"][0]["steps"][0]["builder"] = "dynamic_import"
		with self.assertRaisesRegex(TestDataFactoryContractError, "unsupported"):
			parse_test_data_scenario_registry(payload)

		payload = self._payload()
		payload["scenarios"][0]["steps"][1]["depends_on"] = ["missing-step"]
		with self.assertRaisesRegex(TestDataFactoryContractError, "unknown dependency"):
			parse_test_data_scenario_registry(payload)

	def test_rejects_cyclic_steps_and_duplicate_scenario_ids(self) -> None:
		payload = self._payload()
		payload["scenarios"][0]["steps"][0]["depends_on"] = ["dependency-marker"]
		with self.assertRaisesRegex(TestDataFactoryContractError, "cyclic"):
			parse_test_data_scenario_registry(payload)

		payload = self._payload()
		duplicate = copy.deepcopy(payload["scenarios"][0])
		duplicate["version"] = 2
		payload["scenarios"].append(duplicate)
		with self.assertRaisesRegex(TestDataFactoryContractError, "duplicate scenario IDs"):
			parse_test_data_scenario_registry(payload)

	def test_load_failure_does_not_fall_back_to_an_implicit_scenario(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			path = Path(temp) / "scenarios.json"
			path.write_text("{", encoding="utf-8")
			with self.assertRaisesRegex(TestDataFactoryContractError, "cannot load"):
				load_test_data_scenario_registry(path)


if __name__ == "__main__":
	unittest.main()
