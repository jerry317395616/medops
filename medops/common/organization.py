from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from medops.common.domain_service import fingerprint_json

ORGANIZATION_SCHEMA_VERSION = 1
HOSPITAL_DOCTYPE = "HRP Hospital"
ORGANIZATION_VERSION_DOCTYPE = "HRP Organization Version"
ORGANIZATION_UNIT_DOCTYPE = "HRP Organization Unit"
MAX_HIERARCHY_NODES = 2000
MAX_HIERARCHY_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_CODE_LENGTH = 64
MAX_NAME_LENGTH = 140
MAX_REMARKS_LENGTH = 500
CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_-]{0,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f]")
_MULTILINE_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f]")
UNIT_TYPES = (
	"HOSPITAL",
	"CAMPUS",
	"CLINICAL_DEPARTMENT",
	"ADMINISTRATIVE_DEPARTMENT",
	"NURSING_UNIT",
	"WARD",
	"MEDICAL_GROUP",
	"COST_RESPONSIBILITY_CENTER",
	"POSITION",
	"OTHER",
)
OrganizationUnitType = Literal[
	"HOSPITAL",
	"CAMPUS",
	"CLINICAL_DEPARTMENT",
	"ADMINISTRATIVE_DEPARTMENT",
	"NURSING_UNIT",
	"WARD",
	"MEDICAL_GROUP",
	"COST_RESPONSIBILITY_CENTER",
	"POSITION",
	"OTHER",
]


class OrganizationContractError(ValueError):
	"""Raised when an organization command violates the public contract."""


def normalize_code(value: object, *, label: str) -> str:
	if not isinstance(value, str) or value != value.strip():
		raise OrganizationContractError(f"{label} is invalid")
	normalized = value.upper()
	if len(normalized) > MAX_CODE_LENGTH or CODE_PATTERN.fullmatch(normalized) is None:
		raise OrganizationContractError(f"{label} is invalid")
	return normalized


def normalize_required_text(value: object, *, label: str, maximum: int = MAX_NAME_LENGTH) -> str:
	if not isinstance(value, str):
		raise OrganizationContractError(f"{label} must be text")
	normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
	if (
		not normalized
		or len(normalized) > maximum
		or (
			_CONTROL_CHARACTERS.search(normalized)
			if type(normalized) is str
			else any(ord(character) < 32 for character in normalized)
		)
	):
		raise OrganizationContractError(f"{label} is invalid")
	return normalized


def normalize_optional_text(
	value: object,
	*,
	label: str,
	maximum: int = MAX_REMARKS_LENGTH,
) -> str | None:
	if value in (None, ""):
		return None
	if not isinstance(value, str):
		raise OrganizationContractError(f"{label} must be text")
	normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
	if not normalized:
		return None
	if len(normalized) > maximum or (
		_MULTILINE_CONTROL_CHARACTERS.search(normalized)
		if type(normalized) is str
		else any(ord(character) < 32 and character not in {"\n", "\t"} for character in normalized)
	):
		raise OrganizationContractError(f"{label} is invalid")
	return normalized


def normalize_reference(value: object, *, label: str) -> str:
	return normalize_required_text(value, label=label, maximum=MAX_NAME_LENGTH)


def normalize_boolean(value: object, *, label: str) -> bool:
	if isinstance(value, bool):
		return value
	if isinstance(value, int) and value in (0, 1):
		return bool(value)
	if isinstance(value, str) and value in {"0", "1"}:
		return value == "1"
	raise OrganizationContractError(f"{label} must be boolean")


def normalize_nonnegative_integer(value: object, *, label: str) -> int:
	if isinstance(value, bool):
		raise OrganizationContractError(f"{label} must be an integer")
	if isinstance(value, int):
		normalized = value
	elif isinstance(value, str) and value and value.isascii() and value.isdigit():
		normalized = int(value)
	else:
		raise OrganizationContractError(f"{label} must be an integer")
	if normalized < 0:
		raise OrganizationContractError(f"{label} must not be negative")
	return normalized


def normalize_positive_integer(value: object, *, label: str) -> int:
	normalized = normalize_nonnegative_integer(value, label=label)
	if normalized < 1:
		raise OrganizationContractError(f"{label} must be positive")
	return normalized


def normalize_required_date(value: object, *, label: str) -> str:
	if isinstance(value, date):
		return value.isoformat()
	if not isinstance(value, str) or value != value.strip():
		raise OrganizationContractError(f"{label} is invalid")
	try:
		return date.fromisoformat(value).isoformat()
	except ValueError as exc:
		raise OrganizationContractError(f"{label} is invalid") from exc


def normalize_optional_date(value: object, *, label: str) -> str | None:
	if value in (None, ""):
		return None
	return normalize_required_date(value, label=label)


def validate_date_range(valid_from: str | None, valid_to: str | None) -> None:
	if valid_from and valid_to and valid_to < valid_from:
		raise OrganizationContractError("valid_to cannot be before valid_from")


@dataclass(frozen=True, slots=True)
class HospitalUpsert:
	code: str
	company: str
	display_name: str
	enabled: bool
	valid_from: str | None
	valid_to: str | None
	remarks: str | None
	expected_revision: int

	def as_request_payload(self) -> dict[str, object]:
		return {
			"code": self.code,
			"company": self.company,
			"display_name": self.display_name,
			"enabled": self.enabled,
			"valid_from": self.valid_from,
			"valid_to": self.valid_to,
			"remarks": self.remarks,
			"expected_revision": self.expected_revision,
		}


def build_hospital_upsert(
	*,
	code: object,
	company: object,
	display_name: object,
	enabled: object = True,
	valid_from: object = None,
	valid_to: object = None,
	remarks: object = None,
	expected_revision: object = 0,
) -> HospitalUpsert:
	start = normalize_optional_date(valid_from, label="valid_from")
	end = normalize_optional_date(valid_to, label="valid_to")
	validate_date_range(start, end)
	return HospitalUpsert(
		code=normalize_code(code, label="code"),
		company=normalize_reference(company, label="company"),
		display_name=normalize_required_text(display_name, label="display_name"),
		enabled=normalize_boolean(enabled, label="enabled"),
		valid_from=start,
		valid_to=end,
		remarks=normalize_optional_text(remarks, label="remarks"),
		expected_revision=normalize_nonnegative_integer(
			expected_revision,
			label="expected_revision",
		),
	)


@dataclass(frozen=True, slots=True)
class OrganizationVersionCreate:
	hospital: str
	effective_from: str
	version_label: str
	remarks: str | None

	def as_request_payload(self) -> dict[str, object]:
		return {
			"hospital": self.hospital,
			"effective_from": self.effective_from,
			"version_label": self.version_label,
			"remarks": self.remarks,
		}


def build_organization_version_create(
	*,
	hospital: object,
	effective_from: object,
	version_label: object,
	remarks: object = None,
) -> OrganizationVersionCreate:
	return OrganizationVersionCreate(
		hospital=normalize_code(hospital, label="hospital"),
		effective_from=normalize_required_date(effective_from, label="effective_from"),
		version_label=normalize_required_text(version_label, label="version_label"),
		remarks=normalize_optional_text(remarks, label="remarks"),
	)


@dataclass(frozen=True, slots=True)
class OrganizationNode:
	code: str
	display_name: str
	unit_type: OrganizationUnitType
	parent_code: str | None
	is_group: bool
	enabled: bool
	sequence: int
	valid_from: str | None
	valid_to: str | None
	remarks: str | None

	def as_dict(self) -> dict[str, object]:
		return {
			"code": self.code,
			"display_name": self.display_name,
			"unit_type": self.unit_type,
			"parent_code": self.parent_code,
			"is_group": self.is_group,
			"enabled": self.enabled,
			"sequence": self.sequence,
			"valid_from": self.valid_from,
			"valid_to": self.valid_to,
			"remarks": self.remarks,
		}


def _load_node_payload(value: object) -> list[object]:
	loaded = value
	if isinstance(value, str):
		if len(value.encode("utf-8")) > MAX_HIERARCHY_PAYLOAD_BYTES:
			raise OrganizationContractError("nodes payload is too large")
		try:
			loaded = json.loads(value)
		except json.JSONDecodeError as exc:
			raise OrganizationContractError("nodes must contain valid JSON") from exc
	if not isinstance(loaded, list):
		raise OrganizationContractError("nodes must be a list")
	if not isinstance(value, str):
		try:
			payload_size = len(
				json.dumps(
					loaded,
					ensure_ascii=False,
					separators=(",", ":"),
				).encode("utf-8")
			)
		except (TypeError, ValueError) as exc:
			raise OrganizationContractError("nodes must contain JSON-compatible values") from exc
		if payload_size > MAX_HIERARCHY_PAYLOAD_BYTES:
			raise OrganizationContractError("nodes payload is too large")
	if not loaded or len(loaded) > MAX_HIERARCHY_NODES:
		raise OrganizationContractError("nodes count is outside the allowed range")
	return loaded


def _build_node(value: object) -> OrganizationNode:
	if not isinstance(value, dict):
		raise OrganizationContractError("each node must be an object")
	allowed = {
		"code",
		"display_name",
		"unit_type",
		"parent_code",
		"is_group",
		"enabled",
		"sequence",
		"valid_from",
		"valid_to",
		"remarks",
	}
	if set(value) - allowed:
		raise OrganizationContractError("node contains unsupported fields")
	for required in ("code", "display_name", "unit_type", "is_group"):
		if required not in value:
			raise OrganizationContractError(f"node is missing {required}")
	unit_type = value["unit_type"]
	if not isinstance(unit_type, str) or unit_type not in UNIT_TYPES:
		raise OrganizationContractError("unit_type is invalid")
	parent = value.get("parent_code")
	parent_code = None if parent in (None, "") else normalize_code(parent, label="parent_code")
	start = normalize_optional_date(value.get("valid_from"), label="valid_from")
	end = normalize_optional_date(value.get("valid_to"), label="valid_to")
	validate_date_range(start, end)
	return OrganizationNode(
		code=normalize_code(value["code"], label="code"),
		display_name=normalize_required_text(value["display_name"], label="display_name"),
		unit_type=unit_type,
		parent_code=parent_code,
		is_group=normalize_boolean(value["is_group"], label="is_group"),
		enabled=normalize_boolean(value.get("enabled", True), label="enabled"),
		sequence=normalize_positive_integer(value.get("sequence", 1), label="sequence"),
		valid_from=start,
		valid_to=end,
		remarks=normalize_optional_text(value.get("remarks"), label="remarks"),
	)


def normalize_hierarchy_nodes(value: object) -> tuple[OrganizationNode, ...]:
	nodes = tuple(_build_node(item) for item in _load_node_payload(value))
	by_code = {node.code: node for node in nodes}
	if len(by_code) != len(nodes):
		raise OrganizationContractError("node codes must be unique")
	roots = tuple(node for node in nodes if node.parent_code is None)
	if len(roots) != 1:
		raise OrganizationContractError("hierarchy must contain exactly one root")
	root = roots[0]
	if root.unit_type != "HOSPITAL" or not root.is_group:
		raise OrganizationContractError("root must be a hospital group")

	children: dict[str, list[OrganizationNode]] = {code: [] for code in by_code}
	for node in nodes:
		if node.parent_code is None:
			continue
		if node.parent_code == node.code or node.parent_code not in by_code:
			raise OrganizationContractError("node parent is invalid")
		children[node.parent_code].append(node)
	for code, descendants in children.items():
		if descendants and not by_code[code].is_group:
			raise OrganizationContractError("a leaf node cannot contain children")

	ordered: list[OrganizationNode] = []
	visiting: set[str] = set()
	visited: set[str] = set()

	def visit(node: OrganizationNode) -> None:
		if node.code in visiting:
			raise OrganizationContractError("hierarchy contains a cycle")
		if node.code in visited:
			return
		visiting.add(node.code)
		ordered.append(node)
		for child in sorted(children[node.code], key=lambda item: (item.sequence, item.code)):
			visit(child)
		visiting.remove(node.code)
		visited.add(node.code)

	visit(root)
	if len(visited) != len(nodes):
		raise OrganizationContractError("hierarchy contains disconnected nodes")
	return tuple(ordered)


def hierarchy_digest(nodes: tuple[OrganizationNode, ...]) -> str:
	return fingerprint_json(
		{
			"schema_version": ORGANIZATION_SCHEMA_VERSION,
			"nodes": [node.as_dict() for node in sorted(nodes, key=lambda item: item.code)],
		}
	)


@dataclass(frozen=True, slots=True)
class HierarchyReplace:
	organization_version: str
	expected_revision: int
	nodes: tuple[OrganizationNode, ...]
	digest: str

	def as_request_payload(self) -> dict[str, object]:
		return {
			"organization_version": self.organization_version,
			"expected_revision": self.expected_revision,
			"node_count": len(self.nodes),
			"hierarchy_digest": self.digest,
		}


def build_hierarchy_replace(
	*,
	organization_version: object,
	expected_revision: object,
	nodes: object,
) -> HierarchyReplace:
	normalized_nodes = normalize_hierarchy_nodes(nodes)
	return HierarchyReplace(
		organization_version=normalize_reference(
			organization_version,
			label="organization_version",
		),
		expected_revision=normalize_positive_integer(
			expected_revision,
			label="expected_revision",
		),
		nodes=normalized_nodes,
		digest=hierarchy_digest(normalized_nodes),
	)


@dataclass(frozen=True, slots=True)
class OrganizationVersionPublish:
	organization_version: str
	expected_revision: int

	def as_request_payload(self) -> dict[str, object]:
		return {
			"organization_version": self.organization_version,
			"expected_revision": self.expected_revision,
		}


def build_organization_version_publish(
	*,
	organization_version: object,
	expected_revision: object,
) -> OrganizationVersionPublish:
	return OrganizationVersionPublish(
		organization_version=normalize_reference(
			organization_version,
			label="organization_version",
		),
		expected_revision=normalize_positive_integer(
			expected_revision,
			label="expected_revision",
		),
	)


def normalize_hierarchy_digest(value: object) -> str:
	if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
		raise OrganizationContractError("hierarchy_digest is invalid")
	return value


__all__ = [
	"CODE_PATTERN",
	"HOSPITAL_DOCTYPE",
	"MAX_HIERARCHY_NODES",
	"ORGANIZATION_SCHEMA_VERSION",
	"ORGANIZATION_UNIT_DOCTYPE",
	"ORGANIZATION_VERSION_DOCTYPE",
	"UNIT_TYPES",
	"HierarchyReplace",
	"HospitalUpsert",
	"OrganizationContractError",
	"OrganizationNode",
	"OrganizationUnitType",
	"OrganizationVersionCreate",
	"OrganizationVersionPublish",
	"build_hierarchy_replace",
	"build_hospital_upsert",
	"build_organization_version_create",
	"build_organization_version_publish",
	"hierarchy_digest",
	"normalize_boolean",
	"normalize_code",
	"normalize_hierarchy_digest",
	"normalize_hierarchy_nodes",
	"normalize_nonnegative_integer",
	"normalize_optional_date",
	"normalize_optional_text",
	"normalize_positive_integer",
	"normalize_reference",
	"normalize_required_date",
	"normalize_required_text",
	"validate_date_range",
]
