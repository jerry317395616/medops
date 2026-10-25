from __future__ import annotations

import csv
import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from medops.common.constants import APP_NAME

APP_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = APP_PACKAGE_ROOT.parent
POLICY_PATH = APP_PACKAGE_ROOT / "config" / "change_governance.json"
MODULES_PATH = APP_PACKAGE_ROOT / "modules.txt"
POLICY_KEYS = frozenset(
	{
		"schema_version",
		"app",
		"task_id_pattern",
		"adr_id_pattern",
		"baseline_task_id",
		"task_registry",
		"task_document_directory",
		"change_record_directory",
		"adr_directory",
		"allowed_adr_statuses",
		"allowed_adr_transitions",
		"allowed_change_statuses",
		"allowed_risk_levels",
		"allowed_change_types",
		"required_adr_sections",
		"risk_rules",
	}
)
RISK_RULE_KEYS = frozenset(
	{
		"id",
		"path_globs",
		"minimum_risk",
		"requires_accepted_adr",
		"description",
	}
)
ADR_KEYS = frozenset(
	{
		"id",
		"title",
		"status",
		"date",
		"deciders",
		"task_ids",
		"supersedes",
		"superseded_by",
	}
)
CHANGE_RECORD_KEYS = frozenset(
	{
		"schema_version",
		"task_id",
		"title",
		"status",
		"risk_level",
		"change_types",
		"affected_modules",
		"breaking_change",
		"adr_ids",
		"path_globs",
		"permissions_impact",
		"migration_impact",
		"rollback_plan",
		"security_impact",
		"test_plan",
		"owner",
		"created_on",
		"updated_on",
	}
)
BACKLOG_REQUIRED_COLUMNS = frozenset(
	{
		"task_id",
		"phase",
		"app",
		"epic",
		"title",
		"priority",
		"depends_on",
		"size",
		"deliverables",
		"acceptance_criteria",
		"test_requirements",
		"status",
		"domain_group",
	}
)
CHANGE_RECORD_FILENAME = re.compile(r"^(COD-[0-9]{3})\.json$")
ADR_FILENAME = re.compile(r"^(ADR-[0-9]{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
SAFE_RULE_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
SAFE_PATH_GLOB = re.compile(r"^[A-Za-z0-9_.*/-]+$")
RISK_LEVELS = ("low", "medium", "high", "critical")


class ChangeGovernanceError(ValueError):
	"""Raised when ADR or change-governance source violates the repository contract."""


@dataclass(frozen=True, slots=True)
class RiskRule:
	id: str
	path_globs: tuple[str, ...]
	minimum_risk: str
	requires_accepted_adr: bool
	description: str

	def matches(self, path: str) -> bool:
		return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.path_globs)

	def as_public_dict(self) -> dict[str, object]:
		return {
			"id": self.id,
			"minimum_risk": self.minimum_risk,
			"requires_accepted_adr": self.requires_accepted_adr,
			"path_glob_count": len(self.path_globs),
			"description": self.description,
		}


@dataclass(frozen=True, slots=True)
class ChangePolicy:
	schema_version: int
	app: str
	task_id_pattern: re.Pattern[str]
	adr_id_pattern: re.Pattern[str]
	baseline_task_id: str
	task_registry: str
	task_document_directory: str
	change_record_directory: str
	adr_directory: str
	allowed_adr_statuses: tuple[str, ...]
	allowed_adr_transitions: dict[str, tuple[str, ...]]
	allowed_change_statuses: tuple[str, ...]
	allowed_risk_levels: tuple[str, ...]
	allowed_change_types: tuple[str, ...]
	required_adr_sections: tuple[str, ...]
	risk_rules: tuple[RiskRule, ...]

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": self.schema_version,
			"app": self.app,
			"baseline_task_id": self.baseline_task_id,
			"allowed_adr_statuses": list(self.allowed_adr_statuses),
			"allowed_change_statuses": list(self.allowed_change_statuses),
			"allowed_risk_levels": list(self.allowed_risk_levels),
			"risk_rules": [rule.as_public_dict() for rule in self.risk_rules],
		}


@dataclass(frozen=True, slots=True)
class BacklogTask:
	task_id: str
	title: str
	status: str
	app: str


@dataclass(frozen=True, slots=True)
class ArchitectureDecision:
	id: str
	title: str
	status: str
	decision_date: date
	deciders: tuple[str, ...]
	task_ids: tuple[str, ...]
	supersedes: tuple[str, ...]
	superseded_by: str | None
	body: str
	filename: str

	def as_public_dict(self) -> dict[str, object]:
		return {
			"id": self.id,
			"title": self.title,
			"status": self.status,
			"date": self.decision_date.isoformat(),
			"decider_count": len(self.deciders),
			"task_ids": list(self.task_ids),
			"supersedes": list(self.supersedes),
			"superseded_by": self.superseded_by,
			"body_sha256": hashlib.sha256(self.body.encode()).hexdigest(),
		}


@dataclass(frozen=True, slots=True)
class ChangeRecord:
	schema_version: int
	task_id: str
	title: str
	status: str
	risk_level: str
	change_types: tuple[str, ...]
	affected_modules: tuple[str, ...]
	breaking_change: bool
	adr_ids: tuple[str, ...]
	path_globs: tuple[str, ...]
	permissions_impact: str
	migration_impact: str
	rollback_plan: str
	security_impact: str
	test_plan: tuple[str, ...]
	owner: str
	created_on: date
	updated_on: date
	filename: str

	def covers(self, path: str) -> bool:
		return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.path_globs)

	def as_public_dict(self) -> dict[str, object]:
		return {
			"schema_version": self.schema_version,
			"task_id": self.task_id,
			"title": self.title,
			"status": self.status,
			"risk_level": self.risk_level,
			"change_types": list(self.change_types),
			"affected_modules": list(self.affected_modules),
			"breaking_change": self.breaking_change,
			"adr_ids": list(self.adr_ids),
			"path_glob_count": len(self.path_globs),
			"permissions_impact": self.permissions_impact,
			"migration_impact": self.migration_impact,
			"rollback_plan": self.rollback_plan,
			"security_impact": self.security_impact,
			"test_plan": list(self.test_plan),
			"owner": self.owner,
			"created_on": self.created_on.isoformat(),
			"updated_on": self.updated_on.isoformat(),
		}


@dataclass(frozen=True, slots=True)
class GovernanceReport:
	policy: ChangePolicy
	tasks: tuple[BacklogTask, ...]
	decisions: tuple[ArchitectureDecision, ...]
	changes: tuple[ChangeRecord, ...]
	sha256: str

	@property
	def decision_by_id(self) -> dict[str, ArchitectureDecision]:
		return {decision.id: decision for decision in self.decisions}

	@property
	def change_by_task(self) -> dict[str, ChangeRecord]:
		return {change.task_id: change for change in self.changes}

	def as_public_dict(self) -> dict[str, object]:
		status_counts = {
			status: sum(decision.status == status for decision in self.decisions)
			for status in self.policy.allowed_adr_statuses
		}
		return {
			"status": "ok",
			"schema_version": self.policy.schema_version,
			"app": self.policy.app,
			"task_count": len(self.tasks),
			"completed_task_count": sum(task.status == "Done" for task in self.tasks),
			"change_record_count": len(self.changes),
			"decision_count": len(self.decisions),
			"decision_status_counts": status_counts,
			"accepted_decisions": [
				decision.as_public_dict() for decision in self.decisions if decision.status == "Accepted"
			],
			"sha256": self.sha256,
		}


@dataclass(frozen=True, slots=True)
class ChangedPath:
	status: str
	path: str


@dataclass(frozen=True, slots=True)
class ChangeAssessment:
	task_id: str
	changed_paths: tuple[ChangedPath, ...]
	required_risk: str
	matched_rule_ids: tuple[str, ...]
	accepted_adr_ids: tuple[str, ...]
	sha256: str

	def as_dict(self) -> dict[str, object]:
		return {
			"status": "ok",
			"task_id": self.task_id,
			"changed_file_count": len(self.changed_paths),
			"required_risk": self.required_risk,
			"matched_rule_ids": list(self.matched_rule_ids),
			"accepted_adr_ids": list(self.accepted_adr_ids),
			"sha256": self.sha256,
			"idempotent": True,
		}


def _require_exact_keys(payload: dict[str, Any], expected: frozenset[str], label: str) -> None:
	actual = frozenset(payload)
	if actual != expected:
		missing = sorted(expected - actual)
		extra = sorted(actual - expected)
		raise ChangeGovernanceError(f"{label} keys mismatch; missing={missing}, extra={extra}")


def _require_string(value: object, label: str, *, max_length: int = 2000) -> str:
	if not isinstance(value, str) or not value.strip():
		raise ChangeGovernanceError(f"{label} must be a non-empty string")
	result = value.strip()
	if "\x00" in result or len(result) > max_length:
		raise ChangeGovernanceError(f"{label} is invalid or exceeds {max_length} characters")
	return result


def _require_string_list(
	value: object,
	label: str,
	*,
	allow_empty: bool = False,
	max_items: int = 100,
) -> tuple[str, ...]:
	if not isinstance(value, list):
		raise ChangeGovernanceError(f"{label} must be a list")
	if not allow_empty and not value:
		raise ChangeGovernanceError(f"{label} must not be empty")
	if len(value) > max_items:
		raise ChangeGovernanceError(f"{label} exceeds {max_items} entries")
	result = tuple(_require_string(item, f"{label} item", max_length=500) for item in value)
	if len(set(result)) != len(result):
		raise ChangeGovernanceError(f"{label} contains duplicates")
	return result


def _require_relative_path(value: object, label: str) -> str:
	path = _require_string(value, label, max_length=300).replace("\\", "/")
	if (
		path.startswith("/")
		or path.endswith("/")
		or ".." in Path(path).parts
		or not SAFE_PATH_GLOB.fullmatch(path)
	):
		raise ChangeGovernanceError(f"{label} must be a safe repository-relative path")
	return path


def _parse_date(value: object, label: str) -> date:
	if isinstance(value, date):
		return value
	try:
		return date.fromisoformat(_require_string(value, label, max_length=10))
	except ValueError as exc:
		raise ChangeGovernanceError(f"{label} must use YYYY-MM-DD") from exc


def _compile_pattern(value: object, label: str) -> re.Pattern[str]:
	try:
		return re.compile(_require_string(value, label, max_length=100))
	except re.error as exc:
		raise ChangeGovernanceError(f"{label} is not a valid regular expression") from exc


def load_change_policy(path: Path = POLICY_PATH) -> ChangePolicy:
	if path.is_symlink():
		raise ChangeGovernanceError("Change policy cannot be a symbolic link")
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise ChangeGovernanceError(f"Cannot load change policy: {path.name}") from exc
	if not isinstance(payload, dict):
		raise ChangeGovernanceError("Change policy root must be an object")
	_require_exact_keys(payload, POLICY_KEYS, "Change policy")
	if payload["schema_version"] != 1:
		raise ChangeGovernanceError("Unsupported change policy schema version")
	if payload["app"] != APP_NAME:
		raise ChangeGovernanceError(f"Change policy app must be {APP_NAME}")

	task_id_pattern = _compile_pattern(payload["task_id_pattern"], "task_id_pattern")
	adr_id_pattern = _compile_pattern(payload["adr_id_pattern"], "adr_id_pattern")
	for sample, pattern, label in (
		("COD-008", task_id_pattern, "task_id_pattern"),
		("ADR-0002", adr_id_pattern, "adr_id_pattern"),
	):
		if pattern.fullmatch(sample) is None:
			raise ChangeGovernanceError(f"{label} does not match the required identifier shape")
	baseline_task_id = _require_string(payload["baseline_task_id"], "baseline_task_id", max_length=20)
	if task_id_pattern.fullmatch(baseline_task_id) is None:
		raise ChangeGovernanceError("baseline_task_id is invalid")

	allowed_adr_statuses = _require_string_list(payload["allowed_adr_statuses"], "allowed_adr_statuses")
	raw_transitions = payload["allowed_adr_transitions"]
	if not isinstance(raw_transitions, dict) or set(raw_transitions) != set(allowed_adr_statuses):
		raise ChangeGovernanceError("allowed_adr_transitions must define every ADR status")
	transitions: dict[str, tuple[str, ...]] = {}
	for source, targets in raw_transitions.items():
		normalized = _require_string_list(
			targets,
			f"allowed_adr_transitions.{source}",
			allow_empty=True,
		)
		if any(target not in allowed_adr_statuses or target == source for target in normalized):
			raise ChangeGovernanceError(f"Invalid ADR transition from {source}")
		transitions[source] = normalized

	risk_levels = _require_string_list(payload["allowed_risk_levels"], "allowed_risk_levels")
	if risk_levels != RISK_LEVELS:
		raise ChangeGovernanceError("allowed_risk_levels must be low, medium, high, critical")
	change_types = _require_string_list(payload["allowed_change_types"], "allowed_change_types")
	required_sections = _require_string_list(payload["required_adr_sections"], "required_adr_sections")

	raw_rules = payload["risk_rules"]
	if not isinstance(raw_rules, list) or not raw_rules:
		raise ChangeGovernanceError("risk_rules must be a non-empty list")
	rules: list[RiskRule] = []
	for index, raw_rule in enumerate(raw_rules, start=1):
		if not isinstance(raw_rule, dict):
			raise ChangeGovernanceError(f"risk_rules[{index}] must be an object")
		_require_exact_keys(raw_rule, RISK_RULE_KEYS, f"risk_rules[{index}]")
		rule_id = _require_string(raw_rule["id"], f"risk_rules[{index}].id", max_length=64)
		if SAFE_RULE_ID.fullmatch(rule_id) is None:
			raise ChangeGovernanceError(f"risk_rules[{index}].id is invalid")
		path_globs = tuple(
			_require_relative_path(item, f"risk_rules[{index}].path_globs")
			for item in _require_string_list(raw_rule["path_globs"], f"risk_rules[{index}].path_globs")
		)
		minimum_risk = _require_string(
			raw_rule["minimum_risk"], f"risk_rules[{index}].minimum_risk", max_length=20
		)
		if minimum_risk not in risk_levels:
			raise ChangeGovernanceError(f"risk_rules[{index}].minimum_risk is invalid")
		if not isinstance(raw_rule["requires_accepted_adr"], bool):
			raise ChangeGovernanceError(f"risk_rules[{index}].requires_accepted_adr must be boolean")
		rules.append(
			RiskRule(
				id=rule_id,
				path_globs=path_globs,
				minimum_risk=minimum_risk,
				requires_accepted_adr=raw_rule["requires_accepted_adr"],
				description=_require_string(
					raw_rule["description"],
					f"risk_rules[{index}].description",
					max_length=500,
				),
			)
		)
	if len({rule.id for rule in rules}) != len(rules):
		raise ChangeGovernanceError("risk_rules contains duplicate IDs")

	return ChangePolicy(
		schema_version=1,
		app=APP_NAME,
		task_id_pattern=task_id_pattern,
		adr_id_pattern=adr_id_pattern,
		baseline_task_id=baseline_task_id,
		task_registry=_require_relative_path(payload["task_registry"], "task_registry"),
		task_document_directory=_require_relative_path(
			payload["task_document_directory"], "task_document_directory"
		),
		change_record_directory=_require_relative_path(
			payload["change_record_directory"], "change_record_directory"
		),
		adr_directory=_require_relative_path(payload["adr_directory"], "adr_directory"),
		allowed_adr_statuses=allowed_adr_statuses,
		allowed_adr_transitions=transitions,
		allowed_change_statuses=_require_string_list(
			payload["allowed_change_statuses"], "allowed_change_statuses"
		),
		allowed_risk_levels=risk_levels,
		allowed_change_types=change_types,
		required_adr_sections=required_sections,
		risk_rules=tuple(rules),
	)


def load_backlog_tasks(path: Path, policy: ChangePolicy) -> tuple[BacklogTask, ...]:
	try:
		with path.open(encoding="utf-8-sig", newline="") as handle:
			reader = csv.DictReader(handle)
			if reader.fieldnames is None or set(reader.fieldnames) != BACKLOG_REQUIRED_COLUMNS:
				raise ChangeGovernanceError("Backlog columns do not match the governance schema")
			rows = list(reader)
	except OSError as exc:
		raise ChangeGovernanceError("Cannot load backlog registry") from exc
	tasks: list[BacklogTask] = []
	for index, row in enumerate(rows, start=2):
		task_id = _require_string(row.get("task_id"), f"backlog row {index} task_id", max_length=20)
		if policy.task_id_pattern.fullmatch(task_id) is None:
			raise ChangeGovernanceError(f"backlog row {index} has invalid task_id")
		app = _require_string(row.get("app"), f"backlog row {index} app", max_length=100)
		if app != APP_NAME:
			raise ChangeGovernanceError(f"backlog row {index} app must be {APP_NAME}")
		status = _require_string(row.get("status"), f"backlog row {index} status", max_length=30)
		if status not in {"Not Started", "In Progress", "Done"}:
			raise ChangeGovernanceError(f"backlog row {index} has unsupported status")
		tasks.append(
			BacklogTask(
				task_id=task_id,
				title=_require_string(row.get("title"), f"backlog row {index} title", max_length=200),
				status=status,
				app=app,
			)
		)
	if len({task.task_id for task in tasks}) != len(tasks):
		raise ChangeGovernanceError("Backlog contains duplicate task IDs")
	return tuple(tasks)


def _split_adr_text(text: str, filename: str) -> tuple[dict[str, Any], str]:
	if not text.startswith("---\n"):
		raise ChangeGovernanceError(f"{filename} must start with YAML front matter")
	end = text.find("\n---\n", 4)
	if end < 0:
		raise ChangeGovernanceError(f"{filename} front matter is not terminated")
	try:
		header = yaml.safe_load(text[4:end])
	except yaml.YAMLError as exc:
		raise ChangeGovernanceError(f"{filename} has invalid front matter") from exc
	if not isinstance(header, dict):
		raise ChangeGovernanceError(f"{filename} front matter must be an object")
	raw_body = text[end + 5 :]
	if not raw_body.startswith("\n") or raw_body.startswith("\n\n"):
		raise ChangeGovernanceError(f"{filename} must contain one blank line after front matter")
	body = raw_body[1:]
	if not body.endswith("\n"):
		raise ChangeGovernanceError(f"{filename} must end with a newline")
	return header, body


def parse_adr_text(text: str, filename: str, policy: ChangePolicy) -> ArchitectureDecision:
	filename_match = ADR_FILENAME.fullmatch(filename)
	if filename_match is None:
		raise ChangeGovernanceError(f"Invalid ADR filename: {filename}")
	header, body = _split_adr_text(text, filename)
	_require_exact_keys(header, ADR_KEYS, f"{filename} front matter")
	decision_id = _require_string(header["id"], f"{filename} id", max_length=20)
	if decision_id != filename_match.group(1) or policy.adr_id_pattern.fullmatch(decision_id) is None:
		raise ChangeGovernanceError(f"{filename} ID does not match its filename")
	title = _require_string(header["title"], f"{filename} title", max_length=200)
	status = _require_string(header["status"], f"{filename} status", max_length=30)
	if status not in policy.allowed_adr_statuses:
		raise ChangeGovernanceError(f"{filename} has unsupported ADR status")
	decision_date = _parse_date(header["date"], f"{filename} date")
	if decision_date > date.today():
		raise ChangeGovernanceError(f"{filename} date cannot be in the future")
	deciders = _require_string_list(header["deciders"], f"{filename} deciders", max_items=20)
	task_ids = _require_string_list(header["task_ids"], f"{filename} task_ids", max_items=20)
	if any(policy.task_id_pattern.fullmatch(task_id) is None for task_id in task_ids):
		raise ChangeGovernanceError(f"{filename} contains an invalid task ID")
	supersedes = _require_string_list(
		header["supersedes"], f"{filename} supersedes", allow_empty=True, max_items=20
	)
	if any(policy.adr_id_pattern.fullmatch(item) is None for item in supersedes):
		raise ChangeGovernanceError(f"{filename} contains an invalid supersedes ID")
	raw_superseded_by = header["superseded_by"]
	if raw_superseded_by is None:
		superseded_by = None
	else:
		superseded_by = _require_string(raw_superseded_by, f"{filename} superseded_by", max_length=20)
		if policy.adr_id_pattern.fullmatch(superseded_by) is None:
			raise ChangeGovernanceError(f"{filename} has an invalid superseded_by ID")
	if status == "Superseded" and not superseded_by:
		raise ChangeGovernanceError(f"{filename} must identify superseded_by")
	if status != "Superseded" and superseded_by:
		raise ChangeGovernanceError(f"{filename} may set superseded_by only when Superseded")

	expected_heading = f"# {decision_id} {title}\n"
	if not body.startswith(expected_heading):
		raise ChangeGovernanceError(f"{filename} heading must match its ID and title")
	sections = tuple(line.removeprefix("## ").strip() for line in body.splitlines() if line.startswith("## "))
	if sections != policy.required_adr_sections:
		raise ChangeGovernanceError(f"{filename} ADR sections are missing, extra or out of order")
	for section in policy.required_adr_sections:
		marker = f"## {section}\n"
		start = body.index(marker) + len(marker)
		next_start = body.find("\n## ", start)
		content = body[start : next_start if next_start >= 0 else None].strip()
		if not content:
			raise ChangeGovernanceError(f"{filename} section {section} is empty")
	alternatives_marker = "## 备选方案\n"
	alternatives_start = body.index(alternatives_marker) + len(alternatives_marker)
	alternatives_end = body.find("\n## ", alternatives_start)
	alternatives = body[alternatives_start:alternatives_end]
	if len(re.findall(r"(?m)^[1-9][0-9]*\. ", alternatives)) < 2:
		raise ChangeGovernanceError(f"{filename} must document at least two alternatives")
	return ArchitectureDecision(
		id=decision_id,
		title=title,
		status=status,
		decision_date=decision_date,
		deciders=deciders,
		task_ids=task_ids,
		supersedes=supersedes,
		superseded_by=superseded_by,
		body=body,
		filename=filename,
	)


def load_architecture_decisions(
	directory: Path,
	policy: ChangePolicy,
) -> tuple[ArchitectureDecision, ...]:
	if not directory.is_dir() or directory.is_symlink():
		raise ChangeGovernanceError("ADR directory is missing")
	decisions: list[ArchitectureDecision] = []
	for path in sorted(directory.iterdir(), key=lambda item: item.name):
		if not path.is_file() or path.is_symlink():
			raise ChangeGovernanceError(f"ADR directory contains an unsafe entry: {path.name}")
		try:
			text = path.read_text(encoding="utf-8")
		except OSError as exc:
			raise ChangeGovernanceError(f"Cannot load ADR: {path.name}") from exc
		decisions.append(parse_adr_text(text, path.name, policy))
	if not decisions:
		raise ChangeGovernanceError("ADR directory must contain at least one decision")
	ids = {decision.id for decision in decisions}
	if len(ids) != len(decisions):
		raise ChangeGovernanceError("ADR directory contains duplicate IDs")
	for decision in decisions:
		if decision.id in decision.supersedes:
			raise ChangeGovernanceError(f"{decision.id} cannot supersede itself")
		for prior in decision.supersedes:
			if prior not in ids:
				raise ChangeGovernanceError(f"{decision.id} supersedes unknown ADR {prior}")
		if decision.superseded_by and decision.superseded_by not in ids:
			raise ChangeGovernanceError(
				f"{decision.id} references unknown superseding ADR {decision.superseded_by}"
			)
	for decision in decisions:
		if not decision.superseded_by:
			continue
		successor = next(item for item in decisions if item.id == decision.superseded_by)
		if decision.id not in successor.supersedes:
			raise ChangeGovernanceError(f"{successor.id} must list superseded ADR {decision.id}")
	return tuple(decisions)


def _load_declared_modules(path: Path) -> frozenset[str]:
	try:
		modules = tuple(
			line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
		)
	except OSError as exc:
		raise ChangeGovernanceError("Cannot load modules.txt") from exc
	if len(modules) != 36 or len(set(modules)) != 36:
		raise ChangeGovernanceError("Change governance requires exactly 36 unique modules")
	return frozenset(modules)


def parse_change_record(
	payload: object,
	filename: str,
	policy: ChangePolicy,
	modules: frozenset[str],
) -> ChangeRecord:
	filename_match = CHANGE_RECORD_FILENAME.fullmatch(filename)
	if filename_match is None:
		raise ChangeGovernanceError(f"Invalid change record filename: {filename}")
	if not isinstance(payload, dict):
		raise ChangeGovernanceError(f"{filename} root must be an object")
	_require_exact_keys(payload, CHANGE_RECORD_KEYS, filename)
	if payload["schema_version"] != policy.schema_version:
		raise ChangeGovernanceError(f"{filename} schema_version does not match policy")
	task_id = _require_string(payload["task_id"], f"{filename} task_id", max_length=20)
	if task_id != filename_match.group(1) or policy.task_id_pattern.fullmatch(task_id) is None:
		raise ChangeGovernanceError(f"{filename} task_id does not match its filename")
	status = _require_string(payload["status"], f"{filename} status", max_length=30)
	if status not in policy.allowed_change_statuses:
		raise ChangeGovernanceError(f"{filename} has unsupported change status")
	risk_level = _require_string(payload["risk_level"], f"{filename} risk_level", max_length=20)
	if risk_level not in policy.allowed_risk_levels:
		raise ChangeGovernanceError(f"{filename} has unsupported risk level")
	change_types = _require_string_list(payload["change_types"], f"{filename} change_types", max_items=20)
	if any(change_type not in policy.allowed_change_types for change_type in change_types):
		raise ChangeGovernanceError(f"{filename} contains an unsupported change type")
	affected_modules = _require_string_list(
		payload["affected_modules"], f"{filename} affected_modules", max_items=36
	)
	if any(module not in modules for module in affected_modules):
		raise ChangeGovernanceError(f"{filename} references an undeclared module")
	if not isinstance(payload["breaking_change"], bool):
		raise ChangeGovernanceError(f"{filename} breaking_change must be boolean")
	adr_ids = _require_string_list(payload["adr_ids"], f"{filename} adr_ids", allow_empty=True, max_items=20)
	if any(policy.adr_id_pattern.fullmatch(adr_id) is None for adr_id in adr_ids):
		raise ChangeGovernanceError(f"{filename} contains an invalid ADR ID")
	path_globs = tuple(
		_require_relative_path(item, f"{filename} path_globs")
		for item in _require_string_list(payload["path_globs"], f"{filename} path_globs")
	)
	created_on = _parse_date(payload["created_on"], f"{filename} created_on")
	updated_on = _parse_date(payload["updated_on"], f"{filename} updated_on")
	if updated_on < created_on or updated_on > date.today():
		raise ChangeGovernanceError(f"{filename} dates are inconsistent")
	return ChangeRecord(
		schema_version=policy.schema_version,
		task_id=task_id,
		title=_require_string(payload["title"], f"{filename} title", max_length=200),
		status=status,
		risk_level=risk_level,
		change_types=change_types,
		affected_modules=affected_modules,
		breaking_change=payload["breaking_change"],
		adr_ids=adr_ids,
		path_globs=path_globs,
		permissions_impact=_require_string(payload["permissions_impact"], f"{filename} permissions_impact"),
		migration_impact=_require_string(payload["migration_impact"], f"{filename} migration_impact"),
		rollback_plan=_require_string(payload["rollback_plan"], f"{filename} rollback_plan"),
		security_impact=_require_string(payload["security_impact"], f"{filename} security_impact"),
		test_plan=_require_string_list(payload["test_plan"], f"{filename} test_plan", max_items=30),
		owner=_require_string(payload["owner"], f"{filename} owner", max_length=140),
		created_on=created_on,
		updated_on=updated_on,
		filename=filename,
	)


def load_change_records(
	directory: Path,
	policy: ChangePolicy,
	modules_path: Path,
) -> tuple[ChangeRecord, ...]:
	if not directory.is_dir() or directory.is_symlink():
		raise ChangeGovernanceError("Change record directory is missing")
	modules = _load_declared_modules(modules_path)
	records: list[ChangeRecord] = []
	for path in sorted(directory.iterdir(), key=lambda item: item.name):
		if not path.is_file() or path.is_symlink():
			raise ChangeGovernanceError(f"Change record directory contains an unsafe entry: {path.name}")
		try:
			payload = json.loads(path.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError) as exc:
			raise ChangeGovernanceError(f"Cannot load change record: {path.name}") from exc
		records.append(parse_change_record(payload, path.name, policy, modules))
	if len({record.task_id for record in records}) != len(records):
		raise ChangeGovernanceError("Change records contain duplicate task IDs")
	return tuple(records)


def _validate_cross_references(
	root: Path,
	policy: ChangePolicy,
	tasks: tuple[BacklogTask, ...],
	decisions: tuple[ArchitectureDecision, ...],
	changes: tuple[ChangeRecord, ...],
) -> None:
	task_by_id = {task.task_id: task for task in tasks}
	decision_by_id = {decision.id: decision for decision in decisions}
	change_by_task = {change.task_id: change for change in changes}
	for decision in decisions:
		for task_id in decision.task_ids:
			if task_id not in task_by_id:
				raise ChangeGovernanceError(f"{decision.id} references unknown task {task_id}")
	for change in changes:
		task = task_by_id.get(change.task_id)
		if task is None:
			raise ChangeGovernanceError(f"{change.filename} references an unknown backlog task")
		if change.title != task.title:
			raise ChangeGovernanceError(f"{change.filename} title differs from backlog")
		if task.status == "Done" and change.status not in {"Implemented", "Rolled Back"}:
			raise ChangeGovernanceError(f"{change.filename} status differs from completed backlog task")
		if task.status != "Done" and change.status in {"Implemented", "Rolled Back"}:
			raise ChangeGovernanceError(f"{change.filename} cannot be implemented before backlog is Done")
		task_document = root / policy.task_document_directory / f"{change.task_id}.md"
		if not task_document.is_file() or task_document.is_symlink():
			raise ChangeGovernanceError(f"Missing task document for {change.task_id}")
		first_line = task_document.read_text(encoding="utf-8").splitlines()[0].strip()
		if not first_line.startswith(f"# {change.task_id} ") or len(first_line) <= len(change.task_id) + 3:
			raise ChangeGovernanceError(f"Task document heading is invalid for {change.task_id}")
		for adr_id in change.adr_ids:
			if adr_id not in decision_by_id:
				raise ChangeGovernanceError(f"{change.filename} references unknown ADR {adr_id}")
		if change.breaking_change and not any(
			decision_by_id[adr_id].status == "Accepted" for adr_id in change.adr_ids
		):
			raise ChangeGovernanceError(f"{change.filename} breaking change requires an Accepted ADR")
	for task in tasks:
		if task.status == "Done" and task.task_id not in change_by_task:
			raise ChangeGovernanceError(f"Completed task is missing a change record: {task.task_id}")


def _governance_digest(
	policy: ChangePolicy,
	tasks: tuple[BacklogTask, ...],
	decisions: tuple[ArchitectureDecision, ...],
	changes: tuple[ChangeRecord, ...],
) -> str:
	payload = {
		"policy": policy.as_public_dict(),
		"tasks": [{"task_id": task.task_id, "title": task.title, "status": task.status} for task in tasks],
		"decisions": [decision.as_public_dict() for decision in decisions],
		"changes": [change.as_public_dict() for change in changes],
	}
	return hashlib.sha256(
		json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
	).hexdigest()


def inspect_change_governance(
	root: Path = REPOSITORY_ROOT,
	*,
	policy_path: Path | None = None,
	modules_path: Path | None = None,
) -> GovernanceReport:
	resolved_root = root.resolve()
	policy = load_change_policy(policy_path or resolved_root / APP_NAME / "config" / "change_governance.json")
	tasks = load_backlog_tasks(resolved_root / policy.task_registry, policy)
	decisions = load_architecture_decisions(resolved_root / policy.adr_directory, policy)
	changes = load_change_records(
		resolved_root / policy.change_record_directory,
		policy,
		modules_path or resolved_root / APP_NAME / "modules.txt",
	)
	_validate_cross_references(resolved_root, policy, tasks, decisions, changes)
	return GovernanceReport(
		policy=policy,
		tasks=tasks,
		decisions=decisions,
		changes=changes,
		sha256=_governance_digest(policy, tasks, decisions, changes),
	)


def validate_adr_transition(
	previous: ArchitectureDecision,
	current: ArchitectureDecision,
	all_current: dict[str, ArchitectureDecision],
	policy: ChangePolicy,
) -> None:
	if previous.id != current.id or previous.filename != current.filename:
		raise ChangeGovernanceError("ADR identity and filename are immutable")
	if previous.status == current.status:
		if previous.status != "Proposed" and previous != current:
			raise ChangeGovernanceError(f"{current.id} is immutable in status {current.status}")
		return
	if current.status not in policy.allowed_adr_transitions[previous.status]:
		raise ChangeGovernanceError(
			f"{current.id} cannot transition from {previous.status} to {current.status}"
		)
	if current.status == "Superseded":
		if (
			previous.title != current.title
			or previous.decision_date != current.decision_date
			or previous.deciders != current.deciders
			or previous.task_ids != current.task_ids
			or previous.supersedes != current.supersedes
			or previous.body != current.body
		):
			raise ChangeGovernanceError(f"{current.id} accepted decision content is immutable")
		if current.superseded_by is None:
			raise ChangeGovernanceError(f"{current.id} must identify its superseding ADR")
		successor = all_current.get(current.superseded_by)
		if successor is None or current.id not in successor.supersedes:
			raise ChangeGovernanceError(f"{current.id} superseding ADR is not reciprocal")


def assess_changed_paths(
	report: GovernanceReport,
	changed_paths: tuple[ChangedPath, ...],
	*,
	task_id: str | None = None,
) -> ChangeAssessment:
	if not changed_paths:
		raise ChangeGovernanceError("No changed paths were supplied")
	normalized: list[ChangedPath] = []
	for item in changed_paths:
		status = _require_string(item.status, "changed path status", max_length=3)
		path = _require_relative_path(item.path, "changed path")
		normalized.append(ChangedPath(status=status, path=path))
	changed_record_tasks = {
		match.group(1)
		for item in normalized
		if (match := re.fullmatch(r"changes/(COD-[0-9]{3})\.json", item.path))
	}
	changed_record_status = {
		match.group(1): item.status
		for item in normalized
		if (match := re.fullmatch(r"changes/(COD-[0-9]{3})\.json", item.path))
	}
	task_by_id = {task.task_id: task for task in report.tasks}
	baseline_backfill = report.policy.baseline_task_id in changed_record_tasks and all(
		changed_record_status[candidate].startswith("A")
		and candidate in report.change_by_task
		and candidate in task_by_id
		and (candidate == report.policy.baseline_task_id or task_by_id[candidate].status == "Done")
		for candidate in changed_record_tasks
	)
	if task_id:
		if report.policy.task_id_pattern.fullmatch(task_id) is None:
			raise ChangeGovernanceError("Explicit task ID is invalid")
		if (
			changed_record_tasks
			and changed_record_tasks != {task_id}
			and not (task_id == report.policy.baseline_task_id and baseline_backfill)
		):
			raise ChangeGovernanceError("Changed record does not match the explicit task ID")
	else:
		if len(changed_record_tasks) == 1:
			task_id = next(iter(changed_record_tasks))
		elif baseline_backfill:
			task_id = report.policy.baseline_task_id
		else:
			raise ChangeGovernanceError("A governed change must modify exactly one COD change record")
	if task_id is None:
		raise ChangeGovernanceError("A governed change must resolve to one COD task")
	record = report.change_by_task.get(task_id)
	if record is None:
		raise ChangeGovernanceError(f"Change record not found for {task_id}")
	if record.status not in {"In Progress", "Implemented"}:
		raise ChangeGovernanceError(f"{task_id} is not in an executable change status")
	required_paths = {
		f"changes/{task_id}.json",
		f"{report.policy.task_document_directory}/{task_id}.md",
		report.policy.task_registry,
	}
	actual_paths = {item.path for item in normalized}
	missing_required = sorted(required_paths - actual_paths)
	if missing_required:
		raise ChangeGovernanceError(
			f"{task_id} change set is missing required governance files: {missing_required}"
		)
	uncovered = sorted(path for path in actual_paths if not record.covers(path))
	if uncovered:
		raise ChangeGovernanceError(f"{task_id} change record does not cover paths: {uncovered}")
	if any(
		item.status.startswith("D") and item.path.startswith(f"{report.policy.adr_directory}/")
		for item in normalized
	):
		raise ChangeGovernanceError("ADR files cannot be deleted")

	# Required per-task metadata remains mandatory, covered and hashed, but it must not
	# raise the risk of every substantive change merely because governance is present.
	risk_paths = actual_paths - required_paths
	matched_rules = tuple(
		rule for rule in report.policy.risk_rules if any(rule.matches(path) for path in risk_paths)
	)
	required_risk = max(
		(rule.minimum_risk for rule in matched_rules),
		key=report.policy.allowed_risk_levels.index,
		default="low",
	)
	if report.policy.allowed_risk_levels.index(record.risk_level) < report.policy.allowed_risk_levels.index(
		required_risk
	):
		raise ChangeGovernanceError(
			f"{task_id} declares risk {record.risk_level}, but changed paths require {required_risk}"
		)
	accepted = tuple(
		decision
		for adr_id in record.adr_ids
		if (decision := report.decision_by_id[adr_id]).status == "Accepted"
	)
	requires_accepted = record.breaking_change or any(rule.requires_accepted_adr for rule in matched_rules)
	if requires_accepted and not any(task_id in decision.task_ids for decision in accepted):
		raise ChangeGovernanceError(f"{task_id} requires an Accepted ADR directly linked to the task")
	digest_payload = [
		{"status": item.status, "path": item.path}
		for item in sorted(normalized, key=lambda changed: (changed.path, changed.status))
	]
	return ChangeAssessment(
		task_id=task_id,
		changed_paths=tuple(sorted(normalized, key=lambda item: (item.path, item.status))),
		required_risk=required_risk,
		matched_rule_ids=tuple(rule.id for rule in matched_rules),
		accepted_adr_ids=tuple(decision.id for decision in accepted),
		sha256=hashlib.sha256(
			json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
		).hexdigest(),
	)
