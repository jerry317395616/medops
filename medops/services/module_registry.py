from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from medops.common.constants import APP_NAME

DOMAIN_GROUPS = (
	"Core",
	"Finance",
	"Supply",
	"Asset",
	"People",
	"Project",
	"Governance",
	"Platform",
	"Portal",
	"Other",
)
REQUIRED_MODULE_SUBPACKAGES = ("doctype", "report", "page", "workspace", "services", "api", "tests")


class ModuleRegistryError(ValueError):
	"""Raised when the version-controlled module registry is inconsistent."""


def _validated_text(value: object, field: str, *, max_length: int) -> str:
	if not isinstance(value, str):
		raise ModuleRegistryError(f"{field} must be text")
	result = value.strip()
	if not result:
		raise ModuleRegistryError(f"{field} is required")
	if any(char in result for char in ("\r", "\n", "\x00")):
		raise ModuleRegistryError(f"{field} must be one line")
	if len(result) > max_length:
		raise ModuleRegistryError(f"{field} exceeds {max_length} characters")
	return result


def scrub_module_package(name: str) -> str:
	value = name.strip().lower().replace("&", "and")
	value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
	if not value.startswith("hrp_"):
		value = "hrp_" + value.removeprefix("hrp_")
	if not re.fullmatch(r"hrp_[a-z0-9_]+", value):
		raise ModuleRegistryError(f"Invalid module package: {value}")
	return value


@dataclass(frozen=True, slots=True)
class ModuleSpec:
	sequence: int
	module: str
	package: str
	domain_group: str
	label_cn: str
	enabled_by_default: bool
	description: str

	@classmethod
	def from_mapping(cls, row: Mapping[str, Any], *, row_number: int) -> ModuleSpec:
		prefix = f"modules[{row_number}]"
		if not isinstance(row, Mapping):
			raise ModuleRegistryError(f"{prefix} must be an object")
		sequence = row.get("sequence")
		if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
			raise ModuleRegistryError(f"{prefix}.sequence must be a positive integer")

		module = _validated_text(row.get("module"), f"{prefix}.module", max_length=140)
		if not module.startswith("HRP "):
			raise ModuleRegistryError(f"{prefix}.module must start with 'HRP '")

		package = _validated_text(row.get("package"), f"{prefix}.package", max_length=140)
		expected_package = scrub_module_package(module)
		if package != expected_package:
			raise ModuleRegistryError(f"{prefix}.package must be {expected_package!r} for module {module!r}")

		domain_group = _validated_text(
			row.get("domain_group"),
			f"{prefix}.domain_group",
			max_length=40,
		)
		if domain_group not in DOMAIN_GROUPS:
			raise ModuleRegistryError(f"{prefix}.domain_group must be one of: {', '.join(DOMAIN_GROUPS)}")

		enabled_by_default = row.get("enabled_by_default")
		if not isinstance(enabled_by_default, bool):
			raise ModuleRegistryError(f"{prefix}.enabled_by_default must be true or false")

		return cls(
			sequence=sequence,
			module=module,
			package=package,
			domain_group=domain_group,
			label_cn=_validated_text(row.get("label_cn"), f"{prefix}.label_cn", max_length=140),
			enabled_by_default=enabled_by_default,
			description=_validated_text(
				row.get("description"),
				f"{prefix}.description",
				max_length=500,
			),
		)

	def as_dict(self) -> dict[str, object]:
		return {
			"sequence": self.sequence,
			"module": self.module,
			"package": self.package,
			"domain_group": self.domain_group,
			"label_cn": self.label_cn,
			"enabled_by_default": self.enabled_by_default,
			"description": self.description,
		}


@dataclass(frozen=True, slots=True)
class ModuleRegistry:
	title: str
	modules: tuple[ModuleSpec, ...]

	@classmethod
	def from_mapping(cls, payload: object) -> ModuleRegistry:
		if not isinstance(payload, Mapping):
			raise ModuleRegistryError("module registry root must be an object")
		if payload.get("app") != APP_NAME:
			raise ModuleRegistryError(f"module registry app must be {APP_NAME}")

		title = _validated_text(payload.get("title"), "title", max_length=140)
		rows = payload.get("modules")
		if not isinstance(rows, list):
			raise ModuleRegistryError("modules must be a list")
		modules = tuple(ModuleSpec.from_mapping(row, row_number=index) for index, row in enumerate(rows))

		declared_count = payload.get("module_count")
		if isinstance(declared_count, bool) or not isinstance(declared_count, int):
			raise ModuleRegistryError("module_count must be an integer")
		if declared_count != len(modules):
			raise ModuleRegistryError(
				f"module_count is {declared_count}, but registry contains {len(modules)} modules"
			)

		names = [row.module for row in modules]
		packages = [row.package for row in modules]
		sequences = [row.sequence for row in modules]
		if len(names) != len(set(names)):
			raise ModuleRegistryError("module display names must be unique")
		if len(packages) != len(set(packages)):
			raise ModuleRegistryError("module packages must be unique")
		if len(sequences) != len(set(sequences)):
			raise ModuleRegistryError("module sequences must be unique")
		if sequences != sorted(sequences):
			raise ModuleRegistryError("modules must be ordered by ascending sequence")

		return cls(title=title, modules=modules)

	def with_module(self, module: ModuleSpec) -> ModuleRegistry:
		if module.module in {row.module for row in self.modules}:
			raise FileExistsError(f"Module display name already exists: {module.module}")
		if module.package in {row.package for row in self.modules}:
			raise FileExistsError(f"Module package already exists: {module.package}")
		return ModuleRegistry.from_mapping(
			{
				"app": APP_NAME,
				"title": self.title,
				"module_count": len(self.modules) + 1,
				"modules": [row.as_dict() for row in (*self.modules, module)],
			}
		)

	def as_dict(self) -> dict[str, object]:
		return {
			"app": APP_NAME,
			"title": self.title,
			"module_count": len(self.modules),
			"modules": [row.as_dict() for row in self.modules],
		}


def get_app_root(app_root: Path | None = None) -> Path:
	return (app_root or Path(__file__).resolve().parents[2]).resolve()


def get_registry_path(app_root: Path | None = None) -> Path:
	return get_app_root(app_root) / "architecture" / "module_registry.yaml"


def load_module_registry(app_root: Path | None = None) -> ModuleRegistry:
	path = get_registry_path(app_root)
	if not path.is_file():
		raise FileNotFoundError(f"Module registry not found: {path}")
	try:
		payload = yaml.safe_load(path.read_text(encoding="utf-8"))
	except yaml.YAMLError as exc:
		raise ModuleRegistryError(f"Invalid module registry YAML: {exc}") from exc
	return ModuleRegistry.from_mapping(payload)


def dump_module_registry(registry: ModuleRegistry) -> str:
	return yaml.safe_dump(registry.as_dict(), allow_unicode=True, sort_keys=False)


def load_declared_modules(app_root: Path | None = None) -> tuple[str, ...]:
	modules_path = get_app_root(app_root) / APP_NAME / "modules.txt"
	if not modules_path.is_file():
		raise FileNotFoundError(f"Frappe modules file not found: {modules_path}")
	modules = tuple(
		line.strip() for line in modules_path.read_text(encoding="utf-8").splitlines() if line.strip()
	)
	if len(modules) != len(set(modules)):
		raise ModuleRegistryError("modules.txt contains duplicate module names")
	return modules


def validate_module_source_tree(
	app_root: Path | None = None,
	*,
	expected_module_count: int | None = None,
) -> list[str]:
	root = get_app_root(app_root)
	package_root = root / APP_NAME
	violations: list[str] = []
	if not package_root.is_dir():
		return [f"missing {APP_NAME} package directory"]
	try:
		registry = load_module_registry(root)
		declared = load_declared_modules(root)
	except (FileNotFoundError, ModuleRegistryError) as exc:
		return [str(exc)]

	registered_names = tuple(row.module for row in registry.modules)
	if declared != registered_names:
		violations.append("modules.txt and module_registry.yaml differ in order or content")
	if expected_module_count is not None and len(registry.modules) != expected_module_count:
		violations.append(f"expected {expected_module_count} modules, found {len(registry.modules)}")

	expected_packages = {row.package for row in registry.modules}
	actual_packages = {
		path.name for path in package_root.iterdir() if path.is_dir() and path.name.startswith("hrp_")
	}
	for package in sorted(actual_packages - expected_packages):
		violations.append(f"unregistered module package: {package}")
	for row in registry.modules:
		module_root = package_root / row.package
		if not (module_root / "__init__.py").is_file():
			violations.append(f"missing module package: {row.package}")
			continue
		if not (module_root / "README.md").is_file():
			violations.append(f"missing module README: {row.package}/README.md")
		for subpackage in REQUIRED_MODULE_SUBPACKAGES:
			if not (module_root / subpackage / "__init__.py").is_file():
				violations.append(f"missing {row.package}/{subpackage}/__init__.py")
	return violations
