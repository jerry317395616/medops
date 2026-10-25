from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import tomllib
import yaml
from version_lock import load_lock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))

from medops.services.module_registry import (
	load_module_registry,
	validate_module_source_tree,
)

APP = ROOT
PKG = ROOT / "medops"


def fail(message: str) -> None:
	raise AssertionError(message)


def main() -> None:
	pyproject = tomllib.loads((APP / "pyproject.toml").read_text(encoding="utf-8"))
	if pyproject["project"]["name"] != "medops":
		fail("pyproject project name mismatch")
	load_lock(ROOT / "resolved_versions.lock.json")
	manifest = json.loads((ROOT / "DELIVERY_MANIFEST.json").read_text(encoding="utf-8"))
	counts = manifest.get("counts")
	if not isinstance(counts, dict):
		fail("delivery manifest counts are missing")

	registry = load_module_registry(ROOT)
	module_violations = validate_module_source_tree(ROOT, expected_module_count=36)
	if module_violations:
		fail("; ".join(module_violations))

	for json_file in PKG.rglob("*.json"):
		json.loads(json_file.read_text(encoding="utf-8"))
	for yaml_file in ROOT.rglob("*.yaml"):
		yaml.safe_load(yaml_file.read_text(encoding="utf-8"))

	with (ROOT / "design" / "doctype_catalog.csv").open(encoding="utf-8-sig", newline="") as handle:
		rows = list(csv.DictReader(handle))
	expected_doctypes = counts.get("design_doctypes")
	if len(rows) != expected_doctypes:
		fail(f"expected {expected_doctypes} design DocTypes, got {len(rows)}")
	if {row["app"] for row in rows} != {"medops"}:
		fail("doctype catalog contains non-single-app values")

	with (ROOT / "design" / "field_catalog.csv").open(encoding="utf-8-sig", newline="") as handle:
		field_rows = list(csv.DictReader(handle))
	expected_fields = counts.get("design_fields")
	if len(field_rows) != expected_fields:
		fail(f"expected {expected_fields} design fields, got {len(field_rows)}")

	blueprint_files = list((ROOT / "doctype_blueprints").rglob("*.json"))
	expected_blueprints = counts.get("doctype_blueprints")
	if len(blueprint_files) != expected_blueprints:
		fail(f"expected {expected_blueprints} blueprint files, got {len(blueprint_files)}")

	runtime_doctypes = []
	for path in PKG.glob("*/doctype/*/*.json"):
		payload = json.loads(path.read_text(encoding="utf-8"))
		if payload.get("doctype") == "DocType":
			runtime_doctypes.append(path)
	expected_runtime_doctypes = counts.get("starter_doctypes")
	if len(runtime_doctypes) != expected_runtime_doctypes:
		fail(f"expected {expected_runtime_doctypes} runtime DocTypes, got {len(runtime_doctypes)}")

	print(
		json.dumps(
			{
				"status": "ok",
				"modules": len(registry.modules),
				"doctypes": len(rows),
				"fields": len(field_rows),
				"blueprints": len(blueprint_files),
				"runtime_doctypes": len(runtime_doctypes),
			},
			ensure_ascii=False,
		)
	)


if __name__ == "__main__":
	try:
		main()
	except Exception as exc:
		print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
		raise
