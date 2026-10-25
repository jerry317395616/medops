from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from medops.scaffold.module import create_module_files
from medops.services.module_registry import (
	REQUIRED_MODULE_SUBPACKAGES,
	ModuleRegistry,
	ModuleRegistryError,
	ModuleSpec,
	dump_module_registry,
	load_module_registry,
)


def _create_source_tree(root: Path) -> None:
	registry = ModuleRegistry(
		title="Test HRP",
		modules=(
			ModuleSpec(
				sequence=10,
				module="HRP Foundation",
				package="hrp_foundation",
				domain_group="Core",
				label_cn="基础平台",
				enabled_by_default=True,
				description="测试基础平台。",
			),
		),
	)
	(root / "architecture").mkdir()
	(root / "architecture" / "module_registry.yaml").write_text(
		dump_module_registry(registry),
		encoding="utf-8",
	)
	package_root = root / "medops"
	package_root.mkdir()
	(package_root / "modules.txt").write_text("HRP Foundation\n", encoding="utf-8")
	module_root = package_root / "hrp_foundation"
	module_root.mkdir()
	(module_root / "__init__.py").write_text("", encoding="utf-8")
	(module_root / "README.md").write_text("# HRP Foundation\n", encoding="utf-8")
	for subpackage in REQUIRED_MODULE_SUBPACKAGES:
		path = module_root / subpackage
		path.mkdir()
		(path / "__init__.py").write_text("", encoding="utf-8")


class ModuleScaffoldTest(unittest.TestCase):
	def test_creates_consistent_module_source_atomically(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			_create_source_tree(root)
			result = create_module_files(
				name="HRP Medical Insurance",
				domain_group="Finance",
				label_cn="医保运营",
				description="医保目录、结算、拒付和申诉管理。",
				app_root=root,
			)

			self.assertEqual(result["module_count"], 2)
			self.assertEqual(
				(root / "medops" / "modules.txt").read_text(encoding="utf-8"),
				"HRP Foundation\nHRP Medical Insurance\n",
			)
			registry = load_module_registry(root)
			self.assertEqual(registry.modules[-1].sequence, 11)
			self.assertEqual(registry.modules[-1].package, "hrp_medical_insurance")
			for subpackage in REQUIRED_MODULE_SUBPACKAGES:
				self.assertTrue(
					(root / "medops" / "hrp_medical_insurance" / subpackage / "__init__.py").is_file()
				)

	def test_duplicate_is_rejected_without_changing_source(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			_create_source_tree(root)
			modules_before = (root / "medops" / "modules.txt").read_bytes()
			registry_before = (root / "architecture" / "module_registry.yaml").read_bytes()
			with self.assertRaises(FileExistsError):
				create_module_files(
					name="HRP Foundation",
					domain_group="Core",
					label_cn="基础平台",
					description="重复模块。",
					app_root=root,
				)
			self.assertEqual((root / "medops" / "modules.txt").read_bytes(), modules_before)
			self.assertEqual(
				(root / "architecture" / "module_registry.yaml").read_bytes(),
				registry_before,
			)

	def test_validation_failure_rolls_back_all_generated_files(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			_create_source_tree(root)
			modules_before = (root / "medops" / "modules.txt").read_bytes()
			registry_before = (root / "architecture" / "module_registry.yaml").read_bytes()
			with (
				patch(
					"medops.scaffold.module.validate_module_source_tree",
					side_effect=[[], ["injected validation failure"]],
				),
				self.assertRaisesRegex(ModuleRegistryError, "injected validation failure"),
			):
				create_module_files(
					name="HRP Medical Insurance",
					domain_group="Finance",
					label_cn="医保运营",
					description="医保目录、结算、拒付和申诉管理。",
					app_root=root,
				)

			self.assertFalse((root / "medops" / "hrp_medical_insurance").exists())
			self.assertEqual((root / "medops" / "modules.txt").read_bytes(), modules_before)
			self.assertEqual(
				(root / "architecture" / "module_registry.yaml").read_bytes(),
				registry_before,
			)


if __name__ == "__main__":
	unittest.main()
