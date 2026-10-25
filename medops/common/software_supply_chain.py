from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from medops.common.constants import APP_NAME
from medops.common.domain_service import DomainServiceContractError

SOFTWARE_SUPPLY_CHAIN_SCHEMA_VERSION = 1
APP_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOFTWARE_SUPPLY_CHAIN_POLICY_PATH = APP_PACKAGE_ROOT / "config" / "software_supply_chain.json"
VERSION_LOCK_PATH = APP_PACKAGE_ROOT.parent / "resolved_versions.lock.json"
PACKAGE_JSON_PATH = APP_PACKAGE_ROOT.parent / "package.json"
SUPPLY_CHAIN_ROLES = ("System Manager", "HRP System Manager")
SUPPORTED_SBOM_FORMAT = "CycloneDX JSON"
SUPPORTED_SBOM_SPEC_VERSION = "1.7"
SUPPORTED_BINARY_TOOLS = ("gitleaks", "grype", "cyclonedx_cli")
SUPPORTED_PYTHON_TOOLS = ("bandit", "pip_audit")
SUPPORTED_TOOLS = (*SUPPORTED_PYTHON_TOOLS, "npm", *SUPPORTED_BINARY_TOOLS)
UPSTREAM_COMPONENTS = ("frappe", "erpnext", "hrms")
SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SEMANTIC_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SAFE_ARTIFACT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SAFE_RELATIVE_DIRECTORY_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")
LOCAL_PATH_PATTERNS = (
	re.compile(r"^[A-Za-z]:[\\/]"),
	re.compile(r"^/(?:home|Users|private|tmp|var/tmp)/"),
	re.compile(r"^file://", re.IGNORECASE),
)
ROOT_KEYS = frozenset({"schema_version", "app", "sbom", "execution", "tools", "gates", "exceptions"})
SBOM_KEYS = frozenset(
	{
		"format",
		"spec_version",
		"artifact_name",
		"include_development_dependencies",
		"contains_personal_data",
		"required_components",
	}
)
EXECUTION_KEYS = frozenset(
	{
		"http_write_enabled",
		"site_execution_enabled",
		"production_execution_enabled",
		"artifact_directory",
		"artifact_retention_days",
		"subprocess_environment_allowlist",
	}
)
TOOLS_KEYS = frozenset(SUPPORTED_TOOLS)
VERSION_ONLY_TOOL_KEYS = frozenset({"version"})
BINARY_TOOL_KEYS = frozenset({"version", "linux_asset", "linux_sha256"})
GATE_KEYS = frozenset(
	{
		"bandit_minimum_severity",
		"bandit_minimum_confidence",
		"maximum_bandit_findings",
		"maximum_secret_findings",
		"maximum_pip_audit_vulnerabilities",
		"npm_fail_severities",
		"grype_fail_severities",
		"maximum_grype_database_age_days",
		"maximum_denied_licenses",
		"denied_licenses",
	}
)
EXCEPTION_KEYS = frozenset({"kind", "id", "package", "expires_on", "reason", "approved_by"})
ALLOWED_EXCEPTION_KINDS = ("bandit", "license", "secret", "vulnerability")
SEVERITY_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


class SoftwareSupplyChainContractError(DomainServiceContractError):
	"""Raised when the source policy, SBOM or scan evidence violates the contract."""


def _require_exact_keys(payload: dict[str, Any], expected: frozenset[str], label: str) -> None:
	actual = frozenset(payload)
	if actual != expected:
		missing = sorted(expected - actual)
		extra = sorted(actual - expected)
		raise SoftwareSupplyChainContractError(f"{label} keys mismatch; missing={missing}, extra={extra}")


def _require_string(value: object, label: str, *, max_length: int = 160) -> str:
	if (
		not isinstance(value, str)
		or not value
		or value != value.strip()
		or len(value) > max_length
		or any(ord(character) < 32 for character in value)
	):
		raise SoftwareSupplyChainContractError(f"{label} is invalid")
	return value


def _require_bool(value: object, label: str, *, expected: bool | None = None) -> bool:
	if type(value) is not bool:
		raise SoftwareSupplyChainContractError(f"{label} must be a boolean")
	if expected is not None and value is not expected:
		raise SoftwareSupplyChainContractError(f"{label} must be {str(expected).lower()}")
	return value


def _require_int(value: object, label: str, *, minimum: int, maximum: int) -> int:
	if type(value) is not int or not minimum <= value <= maximum:
		raise SoftwareSupplyChainContractError(f"{label} must be an integer from {minimum} to {maximum}")
	return value


def _require_semantic_version(value: object, label: str) -> str:
	version = _require_string(value, label, max_length=20)
	if SEMANTIC_VERSION_PATTERN.fullmatch(version) is None:
		raise SoftwareSupplyChainContractError(f"{label} must be a semantic version")
	return version


def _require_string_list(
	value: object,
	label: str,
	*,
	minimum: int = 1,
	maximum: int = 32,
	case_sensitive_unique: bool = True,
) -> tuple[str, ...]:
	if not isinstance(value, list) or not minimum <= len(value) <= maximum:
		raise SoftwareSupplyChainContractError(f"{label} must be a bounded list")
	items = tuple(_require_string(item, label, max_length=120) for item in value)
	identity = items if case_sensitive_unique else tuple(item.casefold() for item in items)
	if len(set(identity)) != len(identity):
		raise SoftwareSupplyChainContractError(f"{label} contains duplicate values")
	return items


@dataclass(frozen=True, slots=True)
class SecurityTool:
	name: str
	version: str
	linux_asset: str | None = None
	linux_sha256: str | None = None

	def as_public_dict(self) -> dict[str, object]:
		result: dict[str, object] = {"name": self.name, "version": self.version}
		if self.linux_asset is not None:
			result["linux_asset"] = self.linux_asset
			result["linux_sha256"] = self.linux_sha256
		return result


@dataclass(frozen=True, slots=True)
class SbomPolicy:
	format: str
	spec_version: str
	artifact_name: str
	include_development_dependencies: bool
	contains_personal_data: bool
	required_components: tuple[str, ...]

	def as_public_dict(self) -> dict[str, object]:
		return {
			"format": self.format,
			"spec_version": self.spec_version,
			"artifact_name": self.artifact_name,
			"include_development_dependencies": self.include_development_dependencies,
			"contains_personal_data": self.contains_personal_data,
			"required_components": list(self.required_components),
		}


@dataclass(frozen=True, slots=True)
class SecurityExecutionPolicy:
	http_write_enabled: bool
	site_execution_enabled: bool
	production_execution_enabled: bool
	artifact_directory: str
	artifact_retention_days: int
	subprocess_environment_allowlist: tuple[str, ...]

	def as_public_dict(self) -> dict[str, object]:
		return {
			"http_write_enabled": self.http_write_enabled,
			"site_execution_enabled": self.site_execution_enabled,
			"production_execution_enabled": self.production_execution_enabled,
			"artifact_directory": self.artifact_directory,
			"artifact_retention_days": self.artifact_retention_days,
			"subprocess_environment_allowlist": list(self.subprocess_environment_allowlist),
			"execution_location": "external_ci_or_release_process",
		}


@dataclass(frozen=True, slots=True)
class SecurityGates:
	bandit_minimum_severity: str
	bandit_minimum_confidence: str
	maximum_bandit_findings: int
	maximum_secret_findings: int
	maximum_pip_audit_vulnerabilities: int
	npm_fail_severities: tuple[str, ...]
	grype_fail_severities: tuple[str, ...]
	maximum_grype_database_age_days: int
	maximum_denied_licenses: int
	denied_licenses: tuple[str, ...]

	def as_public_dict(self) -> dict[str, object]:
		return {
			"bandit_minimum_severity": self.bandit_minimum_severity,
			"bandit_minimum_confidence": self.bandit_minimum_confidence,
			"maximum_bandit_findings": self.maximum_bandit_findings,
			"maximum_secret_findings": self.maximum_secret_findings,
			"maximum_pip_audit_vulnerabilities": self.maximum_pip_audit_vulnerabilities,
			"npm_fail_severities": list(self.npm_fail_severities),
			"grype_fail_severities": list(self.grype_fail_severities),
			"maximum_grype_database_age_days": self.maximum_grype_database_age_days,
			"maximum_denied_licenses": self.maximum_denied_licenses,
			"denied_licenses": list(self.denied_licenses),
		}


@dataclass(frozen=True, slots=True)
class SecurityException:
	kind: str
	identifier: str
	package: str
	expires_on: date
	reason: str
	approved_by: str

	def matches(self, kind: str, identifier: str, package: str, *, today: date) -> bool:
		return (
			self.kind == kind
			and self.identifier == identifier
			and self.package == package
			and self.expires_on >= today
		)

	def as_public_dict(self) -> dict[str, str]:
		return {
			"kind": self.kind,
			"id": self.identifier,
			"package": self.package,
			"expires_on": self.expires_on.isoformat(),
			"reason": self.reason,
			"approved_by": self.approved_by,
		}


@dataclass(frozen=True, slots=True)
class SoftwareSupplyChainPolicy:
	schema_version: int
	app: str
	sbom: SbomPolicy
	execution: SecurityExecutionPolicy
	tools: tuple[SecurityTool, ...]
	gates: SecurityGates
	exceptions: tuple[SecurityException, ...]
	sha256: str

	def tool(self, name: str) -> SecurityTool:
		for tool in self.tools:
			if tool.name == name:
				return tool
		raise SoftwareSupplyChainContractError(f"unknown security tool: {name}")

	def as_public_dict(self) -> dict[str, object]:
		return {
			"status": "ok",
			"schema_version": self.schema_version,
			"app": self.app,
			"sha256": self.sha256,
			"sbom": self.sbom.as_public_dict(),
			"execution_policy": self.execution.as_public_dict(),
			"tools": [tool.as_public_dict() for tool in self.tools],
			"gates": self.gates.as_public_dict(),
			"exception_count": len(self.exceptions),
			"exceptions": [exception.as_public_dict() for exception in self.exceptions],
			"idempotency": "read_only_replay_safe",
		}


def _parse_tool(name: str, payload: object) -> SecurityTool:
	if not isinstance(payload, dict):
		raise SoftwareSupplyChainContractError(f"tools.{name} must be an object")
	if name in SUPPORTED_BINARY_TOOLS:
		_require_exact_keys(payload, BINARY_TOOL_KEYS, f"tools.{name}")
		asset = _require_string(payload["linux_asset"], f"tools.{name}.linux_asset", max_length=100)
		if SAFE_ARTIFACT_NAME_PATTERN.fullmatch(asset) is None:
			raise SoftwareSupplyChainContractError(f"tools.{name}.linux_asset is unsafe")
		digest = _require_string(payload["linux_sha256"], f"tools.{name}.linux_sha256", max_length=64)
		if SHA256_PATTERN.fullmatch(digest) is None:
			raise SoftwareSupplyChainContractError(f"tools.{name}.linux_sha256 is invalid")
		return SecurityTool(
			name=name,
			version=_require_semantic_version(payload["version"], f"tools.{name}.version"),
			linux_asset=asset,
			linux_sha256=digest,
		)
	_require_exact_keys(payload, VERSION_ONLY_TOOL_KEYS, f"tools.{name}")
	return SecurityTool(
		name=name,
		version=_require_semantic_version(payload["version"], f"tools.{name}.version"),
	)


def _parse_exception(payload: object, index: int) -> SecurityException:
	label = f"exceptions[{index}]"
	if not isinstance(payload, dict):
		raise SoftwareSupplyChainContractError(f"{label} must be an object")
	_require_exact_keys(payload, EXCEPTION_KEYS, label)
	kind = _require_string(payload["kind"], f"{label}.kind", max_length=20)
	if kind not in ALLOWED_EXCEPTION_KINDS:
		raise SoftwareSupplyChainContractError(f"{label}.kind is unsupported")
	try:
		expires_on = date.fromisoformat(
			_require_string(payload["expires_on"], f"{label}.expires_on", max_length=10)
		)
	except ValueError as exc:
		raise SoftwareSupplyChainContractError(f"{label}.expires_on is invalid") from exc
	return SecurityException(
		kind=kind,
		identifier=_require_string(payload["id"], f"{label}.id", max_length=100),
		package=_require_string(payload["package"], f"{label}.package", max_length=160),
		expires_on=expires_on,
		reason=_require_string(payload["reason"], f"{label}.reason", max_length=500),
		approved_by=_require_string(payload["approved_by"], f"{label}.approved_by", max_length=120),
	)


def parse_software_supply_chain_policy(payload: object) -> SoftwareSupplyChainPolicy:
	if not isinstance(payload, dict):
		raise SoftwareSupplyChainContractError("software supply chain policy must be an object")
	_require_exact_keys(payload, ROOT_KEYS, "software supply chain policy")
	if (
		type(payload["schema_version"]) is not int
		or payload["schema_version"] != SOFTWARE_SUPPLY_CHAIN_SCHEMA_VERSION
	):
		raise SoftwareSupplyChainContractError("unsupported software supply chain schema version")
	if payload["app"] != APP_NAME:
		raise SoftwareSupplyChainContractError(f"software supply chain app must be {APP_NAME}")

	raw_sbom = payload["sbom"]
	if not isinstance(raw_sbom, dict):
		raise SoftwareSupplyChainContractError("sbom must be an object")
	_require_exact_keys(raw_sbom, SBOM_KEYS, "sbom")
	artifact_name = _require_string(raw_sbom["artifact_name"], "sbom.artifact_name", max_length=80)
	if SAFE_ARTIFACT_NAME_PATTERN.fullmatch(artifact_name) is None or not artifact_name.endswith(".cdx.json"):
		raise SoftwareSupplyChainContractError("sbom.artifact_name is unsafe")
	required_components = _require_string_list(
		raw_sbom["required_components"],
		"sbom.required_components",
		maximum=16,
		case_sensitive_unique=False,
	)
	if tuple(required_components) != (APP_NAME, *UPSTREAM_COMPONENTS):
		raise SoftwareSupplyChainContractError(
			f"sbom.required_components must be {(APP_NAME, *UPSTREAM_COMPONENTS)}"
		)
	sbom = SbomPolicy(
		format=_require_string(raw_sbom["format"], "sbom.format", max_length=40),
		spec_version=_require_string(raw_sbom["spec_version"], "sbom.spec_version", max_length=10),
		artifact_name=artifact_name,
		include_development_dependencies=_require_bool(
			raw_sbom["include_development_dependencies"],
			"sbom.include_development_dependencies",
			expected=True,
		),
		contains_personal_data=_require_bool(
			raw_sbom["contains_personal_data"],
			"sbom.contains_personal_data",
			expected=False,
		),
		required_components=required_components,
	)
	if sbom.format != SUPPORTED_SBOM_FORMAT or sbom.spec_version != SUPPORTED_SBOM_SPEC_VERSION:
		raise SoftwareSupplyChainContractError("unsupported SBOM format or specification version")

	raw_execution = payload["execution"]
	if not isinstance(raw_execution, dict):
		raise SoftwareSupplyChainContractError("execution must be an object")
	_require_exact_keys(raw_execution, EXECUTION_KEYS, "execution")
	artifact_directory = _require_string(
		raw_execution["artifact_directory"],
		"execution.artifact_directory",
		max_length=120,
	)
	if (
		SAFE_RELATIVE_DIRECTORY_PATTERN.fullmatch(artifact_directory) is None
		or artifact_directory.startswith(("/", "../"))
		or "/../" in artifact_directory
		or artifact_directory != ".artifacts/security"
	):
		raise SoftwareSupplyChainContractError("execution.artifact_directory is unsafe")
	environment_allowlist = _require_string_list(
		raw_execution["subprocess_environment_allowlist"],
		"execution.subprocess_environment_allowlist",
		maximum=32,
	)
	if tuple(sorted(environment_allowlist)) != environment_allowlist:
		raise SoftwareSupplyChainContractError("execution.subprocess_environment_allowlist must be sorted")
	if any("TOKEN" in item or "SECRET" in item or "PASSWORD" in item for item in environment_allowlist):
		raise SoftwareSupplyChainContractError(
			"execution.subprocess_environment_allowlist contains a credential variable"
		)
	execution = SecurityExecutionPolicy(
		http_write_enabled=_require_bool(
			raw_execution["http_write_enabled"],
			"execution.http_write_enabled",
			expected=False,
		),
		site_execution_enabled=_require_bool(
			raw_execution["site_execution_enabled"],
			"execution.site_execution_enabled",
			expected=False,
		),
		production_execution_enabled=_require_bool(
			raw_execution["production_execution_enabled"],
			"execution.production_execution_enabled",
			expected=False,
		),
		artifact_directory=artifact_directory,
		artifact_retention_days=_require_int(
			raw_execution["artifact_retention_days"],
			"execution.artifact_retention_days",
			minimum=1,
			maximum=90,
		),
		subprocess_environment_allowlist=environment_allowlist,
	)

	raw_tools = payload["tools"]
	if not isinstance(raw_tools, dict):
		raise SoftwareSupplyChainContractError("tools must be an object")
	_require_exact_keys(raw_tools, TOOLS_KEYS, "tools")
	tools = tuple(_parse_tool(name, raw_tools[name]) for name in SUPPORTED_TOOLS)

	raw_gates = payload["gates"]
	if not isinstance(raw_gates, dict):
		raise SoftwareSupplyChainContractError("gates must be an object")
	_require_exact_keys(raw_gates, GATE_KEYS, "gates")
	bandit_severity = _require_string(
		raw_gates["bandit_minimum_severity"],
		"gates.bandit_minimum_severity",
		max_length=10,
	)
	bandit_confidence = _require_string(
		raw_gates["bandit_minimum_confidence"],
		"gates.bandit_minimum_confidence",
		max_length=10,
	)
	if bandit_severity not in SEVERITY_ORDER or bandit_confidence not in SEVERITY_ORDER:
		raise SoftwareSupplyChainContractError("Bandit severity or confidence is unsupported")
	npm_fail_severities = _require_string_list(
		raw_gates["npm_fail_severities"],
		"gates.npm_fail_severities",
		maximum=4,
	)
	if npm_fail_severities != ("critical", "high"):
		raise SoftwareSupplyChainContractError("npm fail severities must be critical and high")
	grype_fail_severities = _require_string_list(
		raw_gates["grype_fail_severities"],
		"gates.grype_fail_severities",
		maximum=4,
	)
	if grype_fail_severities != ("Critical", "High"):
		raise SoftwareSupplyChainContractError("Grype fail severities must be Critical and High")
	gates = SecurityGates(
		bandit_minimum_severity=bandit_severity,
		bandit_minimum_confidence=bandit_confidence,
		maximum_bandit_findings=_require_int(
			raw_gates["maximum_bandit_findings"],
			"gates.maximum_bandit_findings",
			minimum=0,
			maximum=20,
		),
		maximum_secret_findings=_require_int(
			raw_gates["maximum_secret_findings"],
			"gates.maximum_secret_findings",
			minimum=0,
			maximum=0,
		),
		maximum_pip_audit_vulnerabilities=_require_int(
			raw_gates["maximum_pip_audit_vulnerabilities"],
			"gates.maximum_pip_audit_vulnerabilities",
			minimum=0,
			maximum=20,
		),
		npm_fail_severities=npm_fail_severities,
		grype_fail_severities=grype_fail_severities,
		maximum_grype_database_age_days=_require_int(
			raw_gates["maximum_grype_database_age_days"],
			"gates.maximum_grype_database_age_days",
			minimum=1,
			maximum=14,
		),
		maximum_denied_licenses=_require_int(
			raw_gates["maximum_denied_licenses"],
			"gates.maximum_denied_licenses",
			minimum=0,
			maximum=20,
		),
		denied_licenses=_require_string_list(
			raw_gates["denied_licenses"],
			"gates.denied_licenses",
			maximum=32,
			case_sensitive_unique=False,
		),
	)

	raw_exceptions = payload["exceptions"]
	if not isinstance(raw_exceptions, list) or len(raw_exceptions) > 64:
		raise SoftwareSupplyChainContractError("exceptions must be a bounded list")
	exceptions = tuple(_parse_exception(item, index) for index, item in enumerate(raw_exceptions))
	exception_ids = [(exception.kind, exception.identifier, exception.package) for exception in exceptions]
	if len(set(exception_ids)) != len(exception_ids):
		raise SoftwareSupplyChainContractError("exceptions contain duplicate identities")

	canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	return SoftwareSupplyChainPolicy(
		schema_version=SOFTWARE_SUPPLY_CHAIN_SCHEMA_VERSION,
		app=APP_NAME,
		sbom=sbom,
		execution=execution,
		tools=tools,
		gates=gates,
		exceptions=exceptions,
		sha256=sha256(canonical.encode()).hexdigest(),
	)


def load_software_supply_chain_policy(
	path: Path = SOFTWARE_SUPPLY_CHAIN_POLICY_PATH,
) -> SoftwareSupplyChainPolicy:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise SoftwareSupplyChainContractError("cannot load software supply chain policy") from exc
	return parse_software_supply_chain_policy(payload)


def _load_json(path: Path, label: str) -> Any:
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise SoftwareSupplyChainContractError(f"cannot load {label}") from exc


def _component_ref(component: dict[str, Any]) -> str:
	return _require_string(component.get("bom-ref"), "component.bom-ref", max_length=500)


def _upstream_components(version_lock: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
	apps = version_lock.get("apps")
	if not isinstance(apps, dict) or set(apps) != set(UPSTREAM_COMPONENTS):
		raise SoftwareSupplyChainContractError("version lock apps are invalid")
	components: list[dict[str, Any]] = []
	dependencies: list[dict[str, Any]] = []
	refs: dict[str, str] = {}
	for name in UPSTREAM_COMPONENTS:
		item = apps.get(name)
		if not isinstance(item, dict):
			raise SoftwareSupplyChainContractError(f"version lock {name} is invalid")
		repository = _require_string(
			item.get("repository"), f"version lock {name}.repository", max_length=200
		)
		commit = _require_string(item.get("commit"), f"version lock {name}.commit", max_length=40)
		version = _require_string(item.get("version"), f"version lock {name}.version", max_length=40)
		if (
			repository != f"https://github.com/jerry317395616/{name}.git"
			or SHA1_PATTERN.fullmatch(commit) is None
		):
			raise SoftwareSupplyChainContractError(f"version lock {name} source is invalid")
		component_ref = f"pkg:github/jerry317395616/{name}@{commit}"
		refs[name] = component_ref
		components.append(
			{
				"bom-ref": component_ref,
				"type": "framework" if name == "frappe" else "application",
				"name": name,
				"version": version,
				"scope": "required",
				"purl": component_ref,
				"hashes": [{"alg": "SHA-1", "content": commit}],
				"externalReferences": [
					{
						"type": "vcs",
						"url": f"https://github.com/jerry317395616/{name}/tree/{commit}",
					}
				],
				"properties": [
					{"name": "ione:source:branch", "value": str(item.get("branch") or "develop")},
					{"name": "ione:source:locked", "value": "true"},
				],
			}
		)
	dependencies.append({"ref": refs["frappe"], "dependsOn": []})
	dependencies.append({"ref": refs["erpnext"], "dependsOn": [refs["frappe"]]})
	dependencies.append({"ref": refs["hrms"], "dependsOn": [refs["erpnext"], refs["frappe"]]})
	return components, dependencies


def _python_components(pip_audit: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
	dependencies = pip_audit.get("dependencies")
	if not isinstance(dependencies, list):
		raise SoftwareSupplyChainContractError("pip-audit dependencies are invalid")
	components: list[dict[str, Any]] = []
	refs: list[str] = []
	for index, dependency in enumerate(dependencies):
		if not isinstance(dependency, dict):
			raise SoftwareSupplyChainContractError(f"pip-audit dependency {index} is invalid")
		name = _require_string(dependency.get("name"), f"pip-audit dependency {index}.name", max_length=120)
		version = _require_string(
			dependency.get("version"),
			f"pip-audit dependency {index}.version",
			max_length=80,
		)
		vulnerabilities = dependency.get("vulns")
		if not isinstance(vulnerabilities, list):
			raise SoftwareSupplyChainContractError(f"pip-audit dependency {index}.vulns is invalid")
		component_ref = f"pkg:pypi/{quote(name.casefold(), safe='._-')}@{quote(version, safe='._+-')}"
		refs.append(component_ref)
		components.append(
			{
				"bom-ref": component_ref,
				"type": "library",
				"name": name,
				"version": version,
				"scope": "required",
				"purl": component_ref,
			}
		)
	return components, refs


def compose_cyclonedx_sbom(
	npm_sbom: object,
	pip_audit_report: object,
	version_lock: object,
	package_metadata: object,
	*,
	source_commit: str,
	policy: SoftwareSupplyChainPolicy,
) -> dict[str, Any]:
	if SHA1_PATTERN.fullmatch(source_commit) is None:
		raise SoftwareSupplyChainContractError("source commit must be a full Git SHA")
	if not isinstance(npm_sbom, dict) or npm_sbom.get("bomFormat") != "CycloneDX":
		raise SoftwareSupplyChainContractError("npm SBOM must be CycloneDX JSON")
	if npm_sbom.get("specVersion") not in {"1.5", "1.6", "1.7"}:
		raise SoftwareSupplyChainContractError("npm SBOM specification version is unsupported")
	if not isinstance(pip_audit_report, dict):
		raise SoftwareSupplyChainContractError("pip-audit report must be an object")
	if not isinstance(version_lock, dict):
		raise SoftwareSupplyChainContractError("version lock must be an object")
	if not isinstance(package_metadata, dict):
		raise SoftwareSupplyChainContractError("package metadata must be an object")

	raw_components = npm_sbom.get("components", [])
	raw_dependencies = npm_sbom.get("dependencies", [])
	raw_metadata = npm_sbom.get("metadata")
	if not isinstance(raw_components, list) or not isinstance(raw_dependencies, list):
		raise SoftwareSupplyChainContractError("npm SBOM components or dependencies are invalid")
	if not isinstance(raw_metadata, dict) or not isinstance(raw_metadata.get("component"), dict):
		raise SoftwareSupplyChainContractError("npm SBOM metadata component is invalid")

	node_root = json.loads(json.dumps(raw_metadata["component"]))
	node_root["name"] = _require_string(
		package_metadata.get("name"),
		"package metadata.name",
		max_length=120,
	)
	node_root["type"] = "application"
	node_root["scope"] = "excluded"
	node_properties = node_root.setdefault("properties", [])
	if not isinstance(node_properties, list):
		raise SoftwareSupplyChainContractError("npm root component properties are invalid")
	node_properties.append({"name": "ione:dependency-scope", "value": "development"})
	node_root_ref = _component_ref(node_root)

	components = [json.loads(json.dumps(component)) for component in raw_components]
	for index, component in enumerate(components):
		if not isinstance(component, dict):
			raise SoftwareSupplyChainContractError(f"npm component {index} is invalid")
		_component_ref(component)
		component["scope"] = "excluded"
		properties = component.setdefault("properties", [])
		if not isinstance(properties, list):
			raise SoftwareSupplyChainContractError(f"npm component {index} properties are invalid")
		if not any(
			isinstance(item, dict) and item.get("name") == "ione:dependency-scope" for item in properties
		):
			properties.append({"name": "ione:dependency-scope", "value": "development"})
	components.append(node_root)

	upstream, upstream_dependencies = _upstream_components(version_lock)
	python_components, python_refs = _python_components(pip_audit_report)
	components.extend(upstream)
	components.extend(python_components)

	app_version = _require_string(
		version_lock.get("app_version", "0.1.0"),
		"app version",
		max_length=40,
	)
	app_ref = f"pkg:pypi/medops@{quote(app_version, safe='._+-')}"
	app_component = {
		"bom-ref": app_ref,
		"type": "application",
		"name": APP_NAME,
		"version": app_version,
		"scope": "required",
		"purl": app_ref,
		"licenses": [{"license": {"name": "Proprietary - legal review required"}}],
		"externalReferences": [
			{
				"type": "vcs",
				"url": f"https://github.com/jerry317395616/medops/tree/{source_commit}",
			}
		],
		"properties": [
			{"name": "ione:source:commit", "value": source_commit},
			{"name": "ione:policy:sha256", "value": policy.sha256},
			{"name": "ione:contains-personal-data", "value": "false"},
		],
	}
	upstream_refs = [
		f"pkg:github/jerry317395616/{name}@{version_lock['apps'][name]['commit']}"
		for name in UPSTREAM_COMPONENTS
	]
	dependencies = [json.loads(json.dumps(item)) for item in raw_dependencies]
	dependencies.extend(upstream_dependencies)
	dependencies.append(
		{
			"ref": app_ref,
			"dependsOn": sorted([*upstream_refs, node_root_ref, *python_refs]),
		}
	)

	component_refs = [_component_ref(component) for component in components]
	if len(component_refs) != len(set(component_refs)):
		raise SoftwareSupplyChainContractError("composed SBOM contains duplicate component references")
	components.sort(key=_component_ref)
	normalized_dependencies: dict[str, set[str]] = {}
	for index, dependency in enumerate(dependencies):
		if not isinstance(dependency, dict):
			raise SoftwareSupplyChainContractError(f"dependency {index} is invalid")
		ref = _require_string(dependency.get("ref"), f"dependency {index}.ref", max_length=500)
		depends_on = dependency.get("dependsOn", [])
		if not isinstance(depends_on, list):
			raise SoftwareSupplyChainContractError(f"dependency {index}.dependsOn is invalid")
		normalized_dependencies.setdefault(ref, set()).update(
			_require_string(item, f"dependency {index}.dependsOn", max_length=500) for item in depends_on
		)

	result: dict[str, Any] = {
		"$schema": "https://cyclonedx.org/schema/bom-1.7.schema.json",
		"bomFormat": "CycloneDX",
		"specVersion": policy.sbom.spec_version,
		"serialNumber": f"urn:uuid:{uuid5(NAMESPACE_URL, f'medops:{source_commit}:{policy.sha256}')}",
		"version": 1,
		"metadata": {
			"tools": {
				"components": [
					{
						"type": "application",
						"name": "npm",
						"version": policy.tool("npm").version,
						"purl": f"pkg:npm/npm@{policy.tool('npm').version}",
					},
					{
						"type": "application",
						"name": "medops security composer",
						"version": str(SOFTWARE_SUPPLY_CHAIN_SCHEMA_VERSION),
					},
				]
			},
			"component": app_component,
			"properties": [
				{"name": "ione:sbom:policy-sha256", "value": policy.sha256},
				{"name": "ione:sbom:source-commit", "value": source_commit},
			],
		},
		"components": components,
		"dependencies": [
			{"ref": ref, "dependsOn": sorted(depends_on)}
			for ref, depends_on in sorted(normalized_dependencies.items())
		],
	}
	validate_cyclonedx_sbom(result, policy)
	return result


def _iter_strings(value: object):
	if isinstance(value, str):
		yield value
	elif isinstance(value, dict):
		for item in value.values():
			yield from _iter_strings(item)
	elif isinstance(value, list):
		for item in value:
			yield from _iter_strings(item)


def validate_cyclonedx_sbom(
	payload: object,
	policy: SoftwareSupplyChainPolicy,
) -> dict[str, object]:
	if not isinstance(payload, dict):
		raise SoftwareSupplyChainContractError("SBOM must be an object")
	if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != policy.sbom.spec_version:
		raise SoftwareSupplyChainContractError("SBOM format or version is invalid")
	if payload.get("$schema") != "https://cyclonedx.org/schema/bom-1.7.schema.json":
		raise SoftwareSupplyChainContractError("SBOM schema reference is invalid")
	metadata = payload.get("metadata")
	if not isinstance(metadata, dict) or not isinstance(metadata.get("component"), dict):
		raise SoftwareSupplyChainContractError("SBOM metadata component is invalid")
	root_component = metadata["component"]
	if root_component.get("name") != APP_NAME:
		raise SoftwareSupplyChainContractError(f"SBOM root component must be {APP_NAME}")
	components = payload.get("components")
	if not isinstance(components, list) or not 4 <= len(components) <= 20_000:
		raise SoftwareSupplyChainContractError("SBOM component list is invalid")
	refs: set[str] = {_component_ref(root_component)}
	names: set[str] = {str(root_component.get("name"))}
	for index, component in enumerate(components):
		if not isinstance(component, dict):
			raise SoftwareSupplyChainContractError(f"SBOM component {index} is invalid")
		ref = _component_ref(component)
		if ref in refs:
			raise SoftwareSupplyChainContractError("SBOM contains duplicate component references")
		refs.add(ref)
		names.add(_require_string(component.get("name"), f"SBOM component {index}.name", max_length=200))
	missing = [name for name in policy.sbom.required_components if name not in names]
	if missing:
		raise SoftwareSupplyChainContractError(f"SBOM is missing required components: {missing}")
	dependencies = payload.get("dependencies")
	if not isinstance(dependencies, list):
		raise SoftwareSupplyChainContractError("SBOM dependency graph is invalid")
	for index, dependency in enumerate(dependencies):
		if not isinstance(dependency, dict):
			raise SoftwareSupplyChainContractError(f"SBOM dependency {index} is invalid")
		ref = _require_string(dependency.get("ref"), f"SBOM dependency {index}.ref", max_length=500)
		if ref not in refs:
			raise SoftwareSupplyChainContractError("SBOM dependency references an unknown component")
		depends_on = dependency.get("dependsOn", [])
		if not isinstance(depends_on, list) or any(item not in refs for item in depends_on):
			raise SoftwareSupplyChainContractError("SBOM dependency edge references an unknown component")
	for value in _iter_strings(payload):
		if any(pattern.search(value) for pattern in LOCAL_PATH_PATTERNS):
			raise SoftwareSupplyChainContractError("SBOM contains a local filesystem path")
	canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	return {
		"status": "ok",
		"sha256": sha256(canonical.encode()).hexdigest(),
		"component_count": len(components) + 1,
		"dependency_count": len(dependencies),
		"spec_version": policy.sbom.spec_version,
	}


def _exception_matches(
	policy: SoftwareSupplyChainPolicy,
	kind: str,
	identifier: str,
	package: str,
	*,
	today: date,
) -> bool:
	return any(exception.matches(kind, identifier, package, today=today) for exception in policy.exceptions)


def _license_names(component: dict[str, Any]) -> set[str]:
	result: set[str] = set()
	licenses = component.get("licenses", [])
	if not isinstance(licenses, list):
		raise SoftwareSupplyChainContractError("SBOM component licenses are invalid")
	for item in licenses:
		if not isinstance(item, dict):
			raise SoftwareSupplyChainContractError("SBOM component license is invalid")
		license_value = item.get("license")
		if isinstance(license_value, dict):
			name = license_value.get("id") or license_value.get("name")
			if isinstance(name, str) and name:
				result.add(name)
		expression = item.get("expression")
		if isinstance(expression, str) and expression:
			result.add(expression)
	return result


def evaluate_security_reports(
	*,
	policy: SoftwareSupplyChainPolicy,
	sbom: object,
	bandit_report: object,
	gitleaks_report: object,
	pip_audit_report: object,
	npm_audit_report: object,
	grype_report: object,
	source_commit: str,
	now: datetime | None = None,
) -> dict[str, object]:
	if SHA1_PATTERN.fullmatch(source_commit) is None:
		raise SoftwareSupplyChainContractError("source commit must be a full Git SHA")
	now = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
	today = now.date()
	sbom_status = validate_cyclonedx_sbom(sbom, policy)
	violations: list[str] = []

	if not isinstance(bandit_report, dict):
		raise SoftwareSupplyChainContractError("Bandit report must be an object")
	if bandit_report.get("errors") not in ([], None):
		raise SoftwareSupplyChainContractError("Bandit report contains scanner errors")
	bandit_results = bandit_report.get("results")
	if not isinstance(bandit_results, list):
		raise SoftwareSupplyChainContractError("Bandit results are invalid")
	bandit_findings = 0
	for index, finding in enumerate(bandit_results):
		if not isinstance(finding, dict):
			raise SoftwareSupplyChainContractError(f"Bandit finding {index} is invalid")
		severity = _require_string(finding.get("issue_severity"), "Bandit severity", max_length=10)
		confidence = _require_string(
			finding.get("issue_confidence"),
			"Bandit confidence",
			max_length=10,
		)
		test_id = _require_string(finding.get("test_id"), "Bandit test ID", max_length=20)
		filename = _require_string(finding.get("filename"), "Bandit filename", max_length=300)
		if (
			SEVERITY_ORDER.get(severity, 0) >= SEVERITY_ORDER[policy.gates.bandit_minimum_severity]
			and SEVERITY_ORDER.get(confidence, 0) >= SEVERITY_ORDER[policy.gates.bandit_minimum_confidence]
			and not _exception_matches(
				policy,
				"bandit",
				test_id,
				filename.replace("\\", "/"),
				today=today,
			)
		):
			bandit_findings += 1
	if bandit_findings > policy.gates.maximum_bandit_findings:
		violations.append("bandit_findings")

	if not isinstance(gitleaks_report, list):
		raise SoftwareSupplyChainContractError("Gitleaks report must be a list")
	secret_findings = len(gitleaks_report)
	if secret_findings > policy.gates.maximum_secret_findings:
		violations.append("secret_findings")

	if not isinstance(pip_audit_report, dict) or not isinstance(
		pip_audit_report.get("dependencies"),
		list,
	):
		raise SoftwareSupplyChainContractError("pip-audit report is invalid")
	pip_vulnerabilities = 0
	for dependency in pip_audit_report["dependencies"]:
		if not isinstance(dependency, dict) or not isinstance(dependency.get("vulns"), list):
			raise SoftwareSupplyChainContractError("pip-audit dependency is invalid")
		name = _require_string(dependency.get("name"), "pip-audit package", max_length=120)
		for vulnerability in dependency["vulns"]:
			if not isinstance(vulnerability, dict):
				raise SoftwareSupplyChainContractError("pip-audit vulnerability is invalid")
			identifier = _require_string(
				vulnerability.get("id"),
				"pip-audit vulnerability ID",
				max_length=100,
			)
			if not _exception_matches(
				policy,
				"vulnerability",
				identifier,
				name,
				today=today,
			):
				pip_vulnerabilities += 1
	if pip_vulnerabilities > policy.gates.maximum_pip_audit_vulnerabilities:
		violations.append("pip_audit_vulnerabilities")

	if not isinstance(npm_audit_report, dict) or npm_audit_report.get("auditReportVersion") != 2:
		raise SoftwareSupplyChainContractError("npm audit report is invalid")
	npm_metadata = npm_audit_report.get("metadata")
	if not isinstance(npm_metadata, dict) or not isinstance(npm_metadata.get("vulnerabilities"), dict):
		raise SoftwareSupplyChainContractError("npm audit metadata is invalid")
	npm_counts = {
		str(key): _require_int(value, f"npm audit {key}", minimum=0, maximum=1_000_000)
		for key, value in npm_metadata["vulnerabilities"].items()
		if key in {"info", "low", "moderate", "high", "critical", "total"}
	}
	if any(npm_counts.get(severity, 0) for severity in policy.gates.npm_fail_severities):
		violations.append("npm_audit_vulnerabilities")

	if not isinstance(grype_report, dict):
		raise SoftwareSupplyChainContractError("Grype report must be an object")
	descriptor = grype_report.get("descriptor")
	if not isinstance(descriptor, dict) or descriptor.get("version") != policy.tool("grype").version:
		raise SoftwareSupplyChainContractError("Grype tool version does not match policy")
	db = descriptor.get("db")
	if not isinstance(db, dict) or not isinstance(db.get("status"), dict):
		raise SoftwareSupplyChainContractError("Grype database status is missing")
	db_status = db["status"]
	if db_status.get("valid") is not True:
		violations.append("grype_database_invalid")
	built_value = _require_string(db_status.get("built"), "Grype database built time", max_length=40)
	try:
		db_built = datetime.fromisoformat(built_value.replace("Z", "+00:00")).astimezone(timezone.utc)
	except ValueError as exc:
		raise SoftwareSupplyChainContractError("Grype database built time is invalid") from exc
	db_age_seconds = max(0.0, (now - db_built).total_seconds())
	if db_age_seconds > policy.gates.maximum_grype_database_age_days * 86_400:
		violations.append("grype_database_stale")
	grype_matches = grype_report.get("matches")
	if not isinstance(grype_matches, list):
		raise SoftwareSupplyChainContractError("Grype matches are invalid")
	grype_counts = {
		severity: 0 for severity in ("Unknown", "Negligible", "Low", "Medium", "High", "Critical")
	}
	blocking_grype_findings = 0
	for index, match in enumerate(grype_matches):
		if not isinstance(match, dict) or not isinstance(match.get("vulnerability"), dict):
			raise SoftwareSupplyChainContractError(f"Grype match {index} is invalid")
		artifact = match.get("artifact")
		if not isinstance(artifact, dict):
			raise SoftwareSupplyChainContractError(f"Grype artifact {index} is invalid")
		identifier = _require_string(
			match["vulnerability"].get("id"),
			"Grype vulnerability ID",
			max_length=100,
		)
		severity = _require_string(
			match["vulnerability"].get("severity"),
			"Grype severity",
			max_length=20,
		)
		package = _require_string(artifact.get("name"), "Grype package", max_length=160)
		grype_counts[severity] = grype_counts.get(severity, 0) + 1
		if severity in policy.gates.grype_fail_severities and not _exception_matches(
			policy,
			"vulnerability",
			identifier,
			package,
			today=today,
		):
			blocking_grype_findings += 1
	if blocking_grype_findings:
		violations.append("grype_vulnerabilities")

	if not isinstance(sbom, dict):
		raise SoftwareSupplyChainContractError("SBOM must be an object")
	components = sbom.get("components")
	if not isinstance(components, list):
		raise SoftwareSupplyChainContractError("SBOM components are invalid")
	denied_license_findings = 0
	unknown_license_components = 0
	for component in [sbom["metadata"]["component"], *components]:
		licenses = _license_names(component)
		if not licenses:
			unknown_license_components += 1
		for license_name in licenses.intersection(policy.gates.denied_licenses):
			package = _require_string(component.get("name"), "licensed component name", max_length=200)
			if not _exception_matches(
				policy,
				"license",
				license_name,
				package,
				today=today,
			):
				denied_license_findings += 1
	if denied_license_findings > policy.gates.maximum_denied_licenses:
		violations.append("denied_licenses")

	expired_exceptions = sum(exception.expires_on < today for exception in policy.exceptions)
	if expired_exceptions:
		violations.append("expired_exceptions")
	return {
		"schema_version": SOFTWARE_SUPPLY_CHAIN_SCHEMA_VERSION,
		"status": "pass" if not violations else "fail",
		"passed": not violations,
		"source_commit": source_commit,
		"policy_sha256": policy.sha256,
		"sbom": sbom_status,
		"tools": {tool.name: tool.version for tool in policy.tools},
		"findings": {
			"bandit": bandit_findings,
			"secrets": secret_findings,
			"pip_audit": pip_vulnerabilities,
			"npm_audit": npm_counts,
			"grype": grype_counts,
			"grype_blocking": blocking_grype_findings,
			"denied_licenses": denied_license_findings,
			"unknown_license_components": unknown_license_components,
			"expired_exceptions": expired_exceptions,
		},
		"vulnerability_database": {
			"schema_version": db_status.get("schemaVersion"),
			"built": db_built.isoformat().replace("+00:00", "Z"),
			"valid": db_status.get("valid") is True,
			"age_days": round(db_age_seconds / 86_400, 3),
		},
		"violations": violations,
	}


def load_composition_inputs(
	*,
	npm_sbom_path: Path,
	pip_audit_path: Path,
	version_lock_path: Path = VERSION_LOCK_PATH,
	package_json_path: Path = PACKAGE_JSON_PATH,
) -> tuple[Any, Any, Any, Any]:
	version_lock = _load_json(version_lock_path, "version lock")
	version_lock["app_version"] = _require_string(
		_load_app_version(),
		"app version",
		max_length=40,
	)
	return (
		_load_json(npm_sbom_path, "npm SBOM"),
		_load_json(pip_audit_path, "pip-audit report"),
		version_lock,
		_load_json(package_json_path, "package metadata"),
	)


def _load_app_version() -> str:
	try:
		tree = ast.parse((APP_PACKAGE_ROOT / "__init__.py").read_text(encoding="utf-8"))
	except (OSError, SyntaxError) as exc:
		raise SoftwareSupplyChainContractError("cannot load app version") from exc
	for statement in tree.body:
		if (
			isinstance(statement, ast.Assign)
			and len(statement.targets) == 1
			and isinstance(statement.targets[0], ast.Name)
			and statement.targets[0].id == "__version__"
			and isinstance(statement.value, ast.Constant)
		):
			return _require_string(statement.value.value, "app version", max_length=40)
	raise SoftwareSupplyChainContractError("app version is missing")


__all__ = [
	"PACKAGE_JSON_PATH",
	"SOFTWARE_SUPPLY_CHAIN_POLICY_PATH",
	"SOFTWARE_SUPPLY_CHAIN_SCHEMA_VERSION",
	"SUPPLY_CHAIN_ROLES",
	"SUPPORTED_SBOM_SPEC_VERSION",
	"VERSION_LOCK_PATH",
	"SbomPolicy",
	"SecurityException",
	"SecurityExecutionPolicy",
	"SecurityGates",
	"SecurityTool",
	"SoftwareSupplyChainContractError",
	"SoftwareSupplyChainPolicy",
	"compose_cyclonedx_sbom",
	"evaluate_security_reports",
	"load_composition_inputs",
	"load_software_supply_chain_policy",
	"parse_software_supply_chain_policy",
	"validate_cyclonedx_sbom",
]
