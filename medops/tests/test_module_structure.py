from __future__ import annotations

from medops.services.module_registry import (
	load_declared_modules,
	load_module_registry,
	validate_module_source_tree,
)


def test_all_36_declared_modules_have_exact_packages() -> None:
	registry = load_module_registry()
	assert len(registry.modules) == 36
	assert load_declared_modules() == tuple(row.module for row in registry.modules)
	assert validate_module_source_tree(expected_module_count=36) == []
