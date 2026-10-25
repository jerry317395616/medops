from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from medops.services.module_registry import (
	DOMAIN_GROUPS,
	REQUIRED_MODULE_SUBPACKAGES,
	ModuleRegistryError,
	ModuleSpec,
	dump_module_registry,
	get_app_root,
	load_declared_modules,
	load_module_registry,
	scrub_module_package,
	validate_module_source_tree,
)


def _validated_text(value: str, field: str, *, max_length: int) -> str:
	result = value.strip()
	if not result:
		raise ValueError(f"{field} is required")
	if any(char in result for char in ("\r", "\n", "\x00")):
		raise ValueError(f"{field} must be one line")
	if len(result) > max_length:
		raise ValueError(f"{field} exceeds {max_length} characters")
	return result


def _atomic_write(path: Path, content: str) -> None:
	temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
	try:
		temporary.write_text(content, encoding="utf-8")
		os.replace(temporary, path)
	finally:
		temporary.unlink(missing_ok=True)


def _write_module_package(module_root: Path, module: ModuleSpec) -> None:
	for subpackage in REQUIRED_MODULE_SUBPACKAGES:
		path = module_root / subpackage
		path.mkdir(parents=True)
		(path / "__init__.py").write_text("", encoding="utf-8")
	(module_root / "__init__.py").write_text(f'"""{module.module}."""\n', encoding="utf-8")
	(module_root / "README.md").write_text(
		f"# {module.module}\n\n"
		f"**领域组：** {module.domain_group}\n\n"
		f"**中文名称：** {module.label_cn}\n\n"
		f"{module.description}\n",
		encoding="utf-8",
	)


def create_module_files(
	*,
	name: str,
	domain_group: str,
	label_cn: str,
	description: str,
	app_root: Path | None = None,
) -> dict[str, Any]:
	"""Create one version-controlled module inside the single medops app."""
	name = _validated_text(name, "name", max_length=140)
	domain_group = _validated_text(domain_group, "domain_group", max_length=40)
	label_cn = _validated_text(label_cn, "label_cn", max_length=140)
	description = _validated_text(description, "description", max_length=500)
	if not name.startswith("HRP "):
		raise ValueError("Module display name must start with 'HRP '")
	if domain_group not in DOMAIN_GROUPS:
		raise ValueError(f"domain_group must be one of: {', '.join(DOMAIN_GROUPS)}")

	app_root = get_app_root(app_root)
	package_root = app_root / "medops"
	modules_file = package_root / "modules.txt"
	registry_file = app_root / "architecture" / "module_registry.yaml"
	if not modules_file.is_file() or not registry_file.is_file():
		raise FileNotFoundError("medops source tree is incomplete")

	modules = load_declared_modules(app_root)
	registry = load_module_registry(app_root)
	current_violations = validate_module_source_tree(app_root)
	if current_violations:
		raise ModuleRegistryError(
			"Existing module source tree is inconsistent: " + "; ".join(current_violations)
		)

	package = scrub_module_package(name)
	module_root = package_root / package
	if name in modules:
		raise FileExistsError(f"Module display name already exists: {name}")
	if module_root.exists():
		raise FileExistsError(f"Module package already exists: {package}")

	module = ModuleSpec(
		sequence=max((row.sequence for row in registry.modules), default=0) + 1,
		module=name,
		package=package,
		domain_group=domain_group,
		label_cn=label_cn,
		enabled_by_default=True,
		description=description,
	)
	updated_registry = registry.with_module(module)
	original_modules = modules_file.read_text(encoding="utf-8")
	original_registry = registry_file.read_text(encoding="utf-8")
	temporary_module_root = package_root / f".{package}.{uuid.uuid4().hex}.tmp"
	installed_module_root = False
	try:
		_write_module_package(temporary_module_root, module)
		os.replace(temporary_module_root, module_root)
		installed_module_root = True
		_atomic_write(modules_file, "\n".join([*modules, name]) + "\n")
		_atomic_write(registry_file, dump_module_registry(updated_registry))
		violations = validate_module_source_tree(app_root)
		if violations:
			raise ModuleRegistryError(
				"Generated module source tree is inconsistent: " + "; ".join(violations)
			)
	except Exception:
		if installed_module_root and module_root.exists():
			shutil.rmtree(module_root)
		_atomic_write(modules_file, original_modules)
		_atomic_write(registry_file, original_registry)
		raise
	finally:
		if temporary_module_root.exists():
			shutil.rmtree(temporary_module_root)

	return {
		"module": name,
		"package": package,
		"domain_group": domain_group,
		"module_root": str(module_root),
		"module_count": len(updated_registry.modules),
	}
