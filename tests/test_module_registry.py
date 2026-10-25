from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any

from medops.services.module_registry import (
	ModuleRegistry,
	ModuleRegistryError,
	load_module_registry,
	validate_module_source_tree,
)

ROOT = Path(__file__).resolve().parents[1]


class ModuleRegistryTest(unittest.TestCase):
	def test_current_registry_defines_exactly_36_valid_modules(self) -> None:
		registry = load_module_registry(ROOT)
		self.assertEqual(len(registry.modules), 36)
		self.assertEqual(validate_module_source_tree(ROOT, expected_module_count=36), [])
		self.assertEqual(len({row.module for row in registry.modules}), 36)
		self.assertEqual(len({row.package for row in registry.modules}), 36)

	def test_rejects_duplicate_sequences(self) -> None:
		registry = load_module_registry(ROOT)
		payload: dict[str, Any] = registry.as_dict()
		payload["modules"][1]["sequence"] = payload["modules"][0]["sequence"]
		with self.assertRaisesRegex(ModuleRegistryError, "sequences must be unique"):
			ModuleRegistry.from_mapping(payload)

	def test_rejects_package_that_does_not_match_module_name(self) -> None:
		registry = load_module_registry(ROOT)
		payload: dict[str, Any] = copy.deepcopy(registry.as_dict())
		payload["modules"][0]["package"] = "hrp_wrong"
		with self.assertRaisesRegex(ModuleRegistryError, "must be 'hrp_foundation'"):
			ModuleRegistry.from_mapping(payload)

	def test_reports_unregistered_module_package(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			(root / "architecture").mkdir()
			(root / "medops" / "hrp_unregistered").mkdir(parents=True)
			source_registry = ROOT / "architecture" / "module_registry.yaml"
			source_modules = ROOT / "medops" / "modules.txt"
			(root / "architecture" / "module_registry.yaml").write_bytes(source_registry.read_bytes())
			(root / "medops" / "modules.txt").write_bytes(source_modules.read_bytes())
			violations = validate_module_source_tree(root, expected_module_count=36)
			self.assertIn("unregistered module package: hrp_unregistered", violations)


if __name__ == "__main__":
	unittest.main()
