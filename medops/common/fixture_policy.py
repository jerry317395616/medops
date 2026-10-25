from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from medops.common.constants import APP_NAME, CORE_ROLES

APP_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = APP_PACKAGE_ROOT / "config" / "fixture_policy.json"
MODULES_PATH = APP_PACKAGE_ROOT / "modules.txt"
FIXTURE_DIRECTORY = APP_PACKAGE_ROOT / "fixtures"
OWNERSHIP_SOURCES = frozenset({"modules", "core_roles"})
POLICY_KEYS = frozenset(
	{
		"schema_version",
		"app",
		"fixture_auto_order",
		"rules",
		"forbidden_doctypes",
		"volatile_fields",
		"sensitive_field_names",
		"sensitive_value_patterns",
	}
)
RULE_KEYS = frozenset({"doctype", "ownership_field", "ownership_source", "depends_on", "description"})
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]*$")


class FixturePolicyError(ValueError):
	"""Raised when fixture policy or fixture source data violates governance."""


@dataclass(frozen=True)
class FixtureRule:
	doctype: str
	ownership_field: str
	ownership_source: str
	ownership_values: tuple[str, ...]
	depends_on: tuple[str, ...]
	description: str
	filename: str

	def as_frappe_hook(self) -> dict[str, object]:
		return {
			"dt": self.doctype,
			"filters": [[self.ownership_field, "in", list(self.ownership_values)]],
		}

	def as_public_dict(self) -> dict[str, object]:
		return {
			"doctype": self.doctype,
			"ownership_field": self.ownership_field,
			"ownership_source": self.ownership_source,
			"ownership_value_count": len(self.ownership_values),
			"depends_on": list(self.depends_on),
			"filename": self.filename,
		}


@dataclass(frozen=True)
class FixturePolicy:
	schema_version: int
	app: str
	fixture_auto_order: bool
	rules: tuple[FixtureRule, ...]
	forbidden_doctypes: frozenset[str]
	volatile_fields: frozenset[str]
	sensitive_field_names: frozenset[str]
	sensitive_value_patterns: tuple[re.Pattern[str], ...]

	@property
	def expected_filenames(self) -> tuple[str, ...]:
		return tuple(rule.filename for rule in self.rules)

	def get_rule(self, doctype: str) -> FixtureRule:
		for rule in self.rules:
			if rule.doctype == doctype:
				return rule
		raise FixturePolicyError(f"Fixture DocType is not allowlisted: {doctype}")

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": self.schema_version,
			"app": self.app,
			"fixture_auto_order": self.fixture_auto_order,
			"rules": [rule.as_public_dict() for rule in self.rules],
			"forbidden_doctype_count": len(self.forbidden_doctypes),
		}


@dataclass(frozen=True)
class FixtureRepositoryReport:
	files: int
	records: int
	records_by_doctype: dict[str, int]
	sha256: str

	def as_dict(self) -> dict[str, object]:
		return {
			"status": "ok",
			"files": self.files,
			"records": self.records,
			"records_by_doctype": dict(self.records_by_doctype),
			"sha256": self.sha256,
		}


def _require_exact_keys(payload: dict[str, Any], expected: frozenset[str], label: str) -> None:
	actual = frozenset(payload)
	if actual != expected:
		missing = sorted(expected - actual)
		extra = sorted(actual - expected)
		raise FixturePolicyError(f"{label} keys mismatch; missing={missing}, extra={extra}")


def _require_string(value: object, label: str) -> str:
	if not isinstance(value, str) or not value.strip():
		raise FixturePolicyError(f"{label} must be a non-empty string")
	return value.strip()


def _require_string_list(value: object, label: str) -> tuple[str, ...]:
	if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
		raise FixturePolicyError(f"{label} must be a list of non-empty strings")
	normalized = tuple(str(item).strip() for item in value)
	if len(set(normalized)) != len(normalized):
		raise FixturePolicyError(f"{label} contains duplicates")
	return normalized


def _read_modules(path: Path) -> tuple[str, ...]:
	if not path.is_file():
		raise FixturePolicyError(f"Module registry not found: {path.name}")
	modules = tuple(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
	if len(modules) != 36 or len(set(modules)) != 36:
		raise FixturePolicyError("Fixture ownership requires exactly 36 unique modules")
	return modules


def _scrub(value: str) -> str:
	return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def load_fixture_policy(
	path: Path = POLICY_PATH,
	*,
	modules_path: Path = MODULES_PATH,
	core_roles: tuple[str, ...] = CORE_ROLES,
) -> FixturePolicy:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise FixturePolicyError(f"Cannot load fixture policy: {path.name}") from exc
	if not isinstance(payload, dict):
		raise FixturePolicyError("Fixture policy root must be an object")
	_require_exact_keys(payload, POLICY_KEYS, "Fixture policy")
	if payload["schema_version"] != 1:
		raise FixturePolicyError("Unsupported fixture policy schema version")
	if payload["app"] != APP_NAME:
		raise FixturePolicyError(f"Fixture policy app must be {APP_NAME}")
	if payload["fixture_auto_order"] is not True:
		raise FixturePolicyError("fixture_auto_order must be enabled")

	modules = _read_modules(modules_path)
	if len(core_roles) != 5 or len(set(core_roles)) != 5:
		raise FixturePolicyError("Fixture ownership requires exactly five unique core roles")
	ownership_values = {"modules": modules, "core_roles": core_roles}

	raw_rules = payload["rules"]
	if not isinstance(raw_rules, list) or not raw_rules:
		raise FixturePolicyError("Fixture policy rules must be a non-empty list")
	number_width = len(str(len(raw_rules)))
	rules: list[FixtureRule] = []
	seen_doctypes: set[str] = set()
	for index, raw_rule in enumerate(raw_rules, start=1):
		if not isinstance(raw_rule, dict):
			raise FixturePolicyError(f"Fixture rule {index} must be an object")
		_require_exact_keys(raw_rule, RULE_KEYS, f"Fixture rule {index}")
		doctype = _require_string(raw_rule["doctype"], f"Fixture rule {index} doctype")
		if not SAFE_IDENTIFIER.fullmatch(doctype) or doctype in seen_doctypes:
			raise FixturePolicyError(f"Fixture rule {index} has an invalid or duplicate DocType")
		ownership_field = _require_string(
			raw_rule["ownership_field"], f"Fixture rule {index} ownership_field"
		)
		if not re.fullmatch(r"[a-z][a-z0-9_]*", ownership_field):
			raise FixturePolicyError(f"Fixture rule {index} ownership_field is invalid")
		ownership_source = _require_string(
			raw_rule["ownership_source"], f"Fixture rule {index} ownership_source"
		)
		if ownership_source not in OWNERSHIP_SOURCES:
			raise FixturePolicyError(f"Fixture rule {index} ownership source is not supported")
		depends_on = _require_string_list(raw_rule["depends_on"], f"Fixture rule {index} depends_on")
		if any(dependency not in seen_doctypes for dependency in depends_on):
			raise FixturePolicyError(f"Fixture rule {index} dependency must appear earlier")
		description = _require_string(raw_rule["description"], f"Fixture rule {index} description")
		filename = f"{str(index).zfill(number_width)}_{_scrub(doctype)}.json"
		rules.append(
			FixtureRule(
				doctype=doctype,
				ownership_field=ownership_field,
				ownership_source=ownership_source,
				ownership_values=tuple(ownership_values[ownership_source]),
				depends_on=depends_on,
				description=description,
				filename=filename,
			)
		)
		seen_doctypes.add(doctype)

	forbidden_doctypes = frozenset(_require_string_list(payload["forbidden_doctypes"], "forbidden_doctypes"))
	if forbidden_doctypes.intersection(seen_doctypes):
		raise FixturePolicyError("An allowlisted DocType is also forbidden")
	volatile_fields = frozenset(_require_string_list(payload["volatile_fields"], "volatile_fields"))
	sensitive_fields = frozenset(
		_require_string_list(payload["sensitive_field_names"], "sensitive_field_names")
	)
	try:
		sensitive_patterns = tuple(
			re.compile(pattern)
			for pattern in _require_string_list(
				payload["sensitive_value_patterns"], "sensitive_value_patterns"
			)
		)
	except re.error as exc:
		raise FixturePolicyError("Invalid sensitive value pattern") from exc
	return FixturePolicy(
		schema_version=1,
		app=APP_NAME,
		fixture_auto_order=True,
		rules=tuple(rules),
		forbidden_doctypes=forbidden_doctypes,
		volatile_fields=volatile_fields,
		sensitive_field_names=sensitive_fields,
		sensitive_value_patterns=sensitive_patterns,
	)


def get_frappe_fixture_hooks() -> list[dict[str, object]]:
	return [rule.as_frappe_hook() for rule in load_fixture_policy().rules]


def _normalized_field_name(value: str) -> str:
	return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _normalize_value(value: object, policy: FixturePolicy, location: str) -> object:
	if isinstance(value, dict):
		normalized: dict[str, object] = {}
		for key, child in value.items():
			if not isinstance(key, str):
				raise FixturePolicyError(f"{location} contains a non-string field name")
			normalized_key = _normalized_field_name(key)
			if normalized_key in policy.sensitive_field_names:
				raise FixturePolicyError(f"{location}.{key} is a forbidden sensitive field")
			if key in policy.volatile_fields:
				continue
			normalized[key] = _normalize_value(child, policy, f"{location}.{key}")
		return normalized
	if isinstance(value, list):
		return [_normalize_value(child, policy, f"{location}[{index}]") for index, child in enumerate(value)]
	if isinstance(value, str):
		for pattern in policy.sensitive_value_patterns:
			if pattern.search(value):
				raise FixturePolicyError(f"{location} contains a forbidden sensitive value")
	return value


def normalize_fixture_payload(
	policy: FixturePolicy,
	rule: FixtureRule,
	payload: object,
) -> list[dict[str, object]]:
	if not isinstance(payload, list):
		raise FixturePolicyError(f"{rule.filename} root must be a list")
	records: list[dict[str, object]] = []
	seen_names: set[str] = set()
	for index, raw_record in enumerate(payload):
		normalized = _normalize_value(raw_record, policy, f"{rule.filename}[{index}]")
		if not isinstance(normalized, dict):
			raise FixturePolicyError(f"{rule.filename}[{index}] must be an object")
		if normalized.get("doctype") != rule.doctype:
			raise FixturePolicyError(f"{rule.filename}[{index}] has the wrong DocType")
		name = normalized.get("name")
		if not isinstance(name, str) or not name:
			raise FixturePolicyError(f"{rule.filename}[{index}] must have a stable name")
		if name in seen_names:
			raise FixturePolicyError(f"{rule.filename} contains duplicate name {name}")
		owner_value = normalized.get(rule.ownership_field)
		if owner_value not in rule.ownership_values:
			raise FixturePolicyError(f"{rule.filename}[{index}] is not owned through {rule.ownership_field}")
		seen_names.add(name)
		records.append(normalized)
	records.sort(
		key=lambda record: (
			str(record["name"]),
			json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
		)
	)
	return records


def canonical_fixture_text(payload: list[dict[str, object]]) -> str:
	return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def canonicalize_fixture_repository(
	policy: FixturePolicy,
	fixture_directory: Path = FIXTURE_DIRECTORY,
) -> FixtureRepositoryReport:
	_validate_directory_shape(policy, fixture_directory)
	for rule in policy.rules:
		path = fixture_directory / rule.filename
		try:
			payload = json.loads(path.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError) as exc:
			raise FixturePolicyError(f"Cannot parse fixture file: {rule.filename}") from exc
		normalized = normalize_fixture_payload(policy, rule, payload)
		canonical = canonical_fixture_text(normalized)
		if path.read_text(encoding="utf-8") != canonical:
			path.write_text(canonical, encoding="utf-8", newline="\n")
	return inspect_fixture_repository(policy, fixture_directory)


def _validate_directory_shape(policy: FixturePolicy, fixture_directory: Path) -> None:
	if not fixture_directory.is_dir():
		raise FixturePolicyError("Fixture directory is missing")
	expected = set(policy.expected_filenames)
	actual = {path.name for path in fixture_directory.iterdir()}
	if actual != expected:
		raise FixturePolicyError(
			f"Fixture directory mismatch; missing={sorted(expected - actual)}, "
			f"extra={sorted(actual - expected)}"
		)


def inspect_fixture_repository(
	policy: FixturePolicy,
	fixture_directory: Path = FIXTURE_DIRECTORY,
) -> FixtureRepositoryReport:
	_validate_directory_shape(policy, fixture_directory)
	digest = hashlib.sha256()
	records_by_doctype: dict[str, int] = {}
	for rule in policy.rules:
		path = fixture_directory / rule.filename
		try:
			text = path.read_text(encoding="utf-8")
			payload = json.loads(text)
		except (OSError, json.JSONDecodeError) as exc:
			raise FixturePolicyError(f"Cannot parse fixture file: {rule.filename}") from exc
		normalized = normalize_fixture_payload(policy, rule, payload)
		canonical = canonical_fixture_text(normalized)
		if text != canonical:
			raise FixturePolicyError(f"Fixture file is not canonical: {rule.filename}")
		records_by_doctype[rule.doctype] = len(normalized)
		digest.update(rule.filename.encode())
		digest.update(b"\0")
		digest.update(canonical.encode())
	return FixtureRepositoryReport(
		files=len(policy.rules),
		records=sum(records_by_doctype.values()),
		records_by_doctype=records_by_doctype,
		sha256=digest.hexdigest(),
	)
