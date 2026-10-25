from __future__ import annotations

import ast
import csv
import json
import re
import sys
from pathlib import Path

import tomllib
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
	sys.path.insert(0, str(REPOSITORY_ROOT))

from medops.common.access_scope import (
	ACCESS_ACTIONS,
	ACCESS_SCOPE_DOCTYPE,
	ACCESS_SCOPE_LEVELS,
	ACCESS_SCOPE_MEMBER_DOCTYPE,
	ACCESS_SCOPE_SCHEMA_VERSION,
)
from medops.common.approval_matrix import (
	APPROVAL_MATRIX_DOCTYPE,
	APPROVAL_MATRIX_ROW_DOCTYPE,
	APPROVAL_MATRIX_SCHEMA_VERSION,
	APPROVAL_MODES,
	APPROVER_TYPES,
)
from medops.common.change_governance import (
	ChangeGovernanceError,
	inspect_change_governance,
)
from medops.common.data_quality import (
	DATA_QUALITY_ISSUE_DOCTYPE,
	DATA_QUALITY_RULE_DOCTYPE,
	DATA_QUALITY_SCHEMA_VERSION,
	NAMED_PATTERNS,
	RULE_TYPES,
)
from medops.common.delegation import (
	DELEGATION_DOCTYPE,
	DELEGATION_SCHEMA_VERSION,
	DELEGATION_STATUSES,
	MAX_DELEGATION_DAYS,
)
from medops.common.environment_profiles import (
	EnvironmentProfileError,
	load_environment_registry,
)
from medops.common.error_catalog import (
	ErrorCatalogError,
	load_error_catalog,
	validate_error_translations,
)
from medops.common.external_code_mapping import (
	EXTERNAL_CODE_MAPPING_DOCTYPE,
	EXTERNAL_CODE_MAPPING_SCHEMA_VERSION,
)
from medops.common.fixture_policy import (
	FixturePolicyError,
	inspect_fixture_repository,
	load_fixture_policy,
)
from medops.common.immutable_ledger import BASE_LEDGER_FIELDS
from medops.common.master_data import (
	MASTER_DATA_CHANGE_ITEM_DOCTYPE,
	MASTER_DATA_DOMAIN_DOCTYPE,
	MASTER_DATA_REQUEST_DOCTYPE,
	MASTER_DATA_SCHEMA_VERSION,
	MASTER_DATA_TARGET_POLICIES,
)
from medops.common.numbering import (
	MAX_DIMENSIONS,
	MAX_SEQUENCE_DIGITS,
	MAX_TEMPLATE_LENGTH,
	MIN_SEQUENCE_DIGITS,
	NUMBER_RESERVATION_DOCTYPE,
	NUMBERING_SCHEMA_VERSION,
	NUMBERING_SCHEME_DOCTYPE,
	RESET_POLICIES,
)
from medops.common.organization import (
	MAX_HIERARCHY_NODES,
	ORGANIZATION_SCHEMA_VERSION,
	UNIT_TYPES,
)
from medops.common.organization_mapping import (
	ORGANIZATION_MAPPING_DOCTYPE,
	ORGANIZATION_MAPPING_SCHEMA_VERSION,
)
from medops.common.performance_baseline import (
	PerformanceBaselineContractError,
	load_performance_baseline_registry,
)
from medops.common.software_supply_chain import (
	SoftwareSupplyChainContractError,
	load_software_supply_chain_policy,
)
from medops.common.system_settings import (
	LOCKED_RELEASE_CHANNEL,
	MAX_INTEGRATION_TIMEOUT_SECONDS,
	MIN_INTEGRATION_TIMEOUT_SECONDS,
)
from medops.common.test_data_factory import (
	TestDataFactoryContractError,
	load_test_data_scenario_registry,
)
from medops.common.transactional_message import BASE_MESSAGE_FIELDS
from medops.services.module_registry import validate_module_source_tree

if __package__:
	from scripts.version_lock import UPSTREAM_APPS, load_lock
else:
	from version_lock import UPSTREAM_APPS, load_lock

APP_NAME = "medops"
UPSTREAM_APPS = frozenset({"frappe", "erpnext", "hrms"})
REQUIRED_CI_CONTEXT = "Required"
REQUIRED_CI_JOBS = frozenset({"quality", "security", "integration", "required"})
REQUIRED_PYTHON_DEV_DEPENDENCIES = frozenset(
	{
		"bandit==1.9.4",
		"pip-audit==2.10.1",
		"pyyaml==6.0.3",
		"ruff==0.15.9",
	}
)
LEGACY_PREFIXES = ("myi" + "_hrp", "myi" + "-hrp")
PROTECTED_LEDGER_PATTERNS = (
	re.compile(r"""(?:new_doc|get_doc)\(\s*["']GL Entry["']"""),
	re.compile(r"""(?:new_doc|get_doc)\(\s*["']Stock Ledger Entry["']"""),
	re.compile(r"""(?:new_doc|get_doc)\(\s*["']Bin["']"""),
	re.compile(r"""tab(?:GL Entry|Stock Ledger Entry|Bin)"""),
)
IMMUTABLE_LEDGER_FORBIDDEN_PERMISSIONS = (
	"create",
	"write",
	"delete",
	"submit",
	"cancel",
	"amend",
	"import",
)
REQUIRED_NODE_DEV_DEPENDENCIES = frozenset(
	{
		"@eslint/js",
		"eslint",
		"eslint-config-prettier",
		"eslint-plugin-vue",
		"globals",
		"prettier",
		"pyright",
		"typescript",
		"typescript-eslint",
	}
)
REQUIRED_NODE_SCRIPTS = frozenset(
	{
		"format:frontend",
		"format:frontend:check",
		"lint:frontend",
		"quality",
		"quality:node",
		"typecheck",
	}
)
EXCLUDED_REPOSITORY_PARTS = frozenset(
	{
		".git",
		".mypy_cache",
		".pyright",
		".pytest_cache",
		".ruff_cache",
		".venv",
		"__pycache__",
		"build",
		"dist",
		"node_modules",
		"venv",
	}
)
TEXT_SUFFIXES = {
	".csv",
	".html",
	".json",
	".md",
	".py",
	".sh",
	".svg",
	".toml",
	".txt",
	".yaml",
	".yml",
}
BASELINE_ERROR_CODES = {
	"AUTHENTICATION_REQUIRED": "IONE-CORE-0001",
	"PERMISSION_DENIED": "IONE-CORE-0002",
	"INVALID_REQUEST": "IONE-CORE-0003",
	"RESOURCE_NOT_FOUND": "IONE-CORE-0004",
	"CONFLICT": "IONE-CORE-0005",
	"INVALID_STATE_TRANSITION": "IONE-CORE-0006",
	"IDEMPOTENCY_CONFLICT": "IONE-CORE-0007",
	"OPERATION_NOT_ALLOWED": "IONE-CORE-0008",
	"CONFIGURATION_INVALID": "IONE-CORE-0009",
	"DEPENDENCY_UNAVAILABLE": "IONE-CORE-0010",
	"RATE_LIMITED": "IONE-CORE-0011",
	"INTERNAL_ERROR": "IONE-CORE-0012",
}


def discover_custom_apps(root: Path) -> dict[str, Path]:
	discovered: dict[str, Path] = {}
	for pyproject_path in root.rglob("pyproject.toml"):
		if EXCLUDED_REPOSITORY_PARTS.intersection(pyproject_path.parts):
			continue
		payload = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
		project_name = payload.get("project", {}).get("name")
		if project_name:
			discovered[str(project_name)] = pyproject_path.parent
	return discovered


def iter_repository_text_files(root: Path):
	for path in root.rglob("*"):
		if (
			not path.is_file()
			or EXCLUDED_REPOSITORY_PARTS.intersection(path.parts)
			or path.name in {"SHA256SUMS.txt", ".eslintcache"}
		):
			continue
		if path.suffix.lower() in TEXT_SUFFIXES:
			yield path


def find_legacy_prefix_references(root: Path) -> list[str]:
	findings: list[str] = []
	for path in iter_repository_text_files(root):
		text = path.read_text(encoding="utf-8")
		for prefix in LEGACY_PREFIXES:
			if prefix in text:
				findings.append(f"{path.relative_to(root)} contains {prefix}")
	return findings


def validate_branch_policy(root: Path) -> list[str]:
	policy_path = root / ".github" / "branch-protection.json"
	if not policy_path.is_file():
		return ["missing .github/branch-protection.json"]
	policy = json.loads(policy_path.read_text(encoding="utf-8"))
	protection = policy.get("protection", {})
	reviews = protection.get("required_pull_request_reviews")
	violations: list[str] = []
	if policy.get("default_branch") != "main":
		violations.append("default branch must be main")
	if protection.get("enforce_admins") is not True:
		violations.append("branch protection must include administrators")
	required_checks = protection.get("required_status_checks")
	if not isinstance(required_checks, dict):
		violations.append("branch protection must require CI status checks")
	else:
		if required_checks.get("strict") is not True:
			violations.append("required CI checks must use the latest main branch")
		if required_checks.get("contexts") != [REQUIRED_CI_CONTEXT]:
			violations.append(f"branch protection must require only {REQUIRED_CI_CONTEXT}")
	if not isinstance(reviews, dict):
		violations.append("pull requests must be required before merging")
	if protection.get("allow_force_pushes") is not False:
		violations.append("force pushes must be disabled")
	if protection.get("allow_deletions") is not False:
		violations.append("protected branch deletion must be disabled")
	return violations


def validate_push_guard(root: Path) -> list[str]:
	hook_path = root / ".githooks" / "pre-push"
	if not hook_path.is_file():
		return ["missing .githooks/pre-push"]
	hook = hook_path.read_text(encoding="utf-8")
	violations: list[str] = []
	if "refs/heads/main" not in hook:
		violations.append("pre-push hook must guard refs/heads/main")
	if "exit 1" not in hook:
		violations.append("pre-push hook must reject direct main pushes")
	if not (root / "scripts" / "apply_branch_protection.py").is_file():
		violations.append("missing scripts/apply_branch_protection.py")
	return violations


def validate_quality_tooling(root: Path) -> list[str]:
	violations: list[str] = []
	required_files = (
		".npmrc",
		".prettierignore",
		".prettierrc.json",
		"eslint.config.mjs",
		"package-lock.json",
		"package.json",
		"pyrightconfig.json",
		"scripts/quality.py",
		"scripts/quality.sh",
	)
	for relative_path in required_files:
		if not (root / relative_path).is_file():
			violations.append(f"missing quality configuration: {relative_path}")
	if violations:
		return violations

	pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
	dev_extras = pyproject.get("project", {}).get("optional-dependencies", {}).get("dev", [])
	if set(dev_extras) != REQUIRED_PYTHON_DEV_DEPENDENCIES:
		violations.append(
			"project.optional-dependencies.dev must be exactly "
			+ ", ".join(sorted(REQUIRED_PYTHON_DEV_DEPENDENCIES))
		)
	ruff = pyproject.get("tool", {}).get("ruff", {})
	if ruff.get("target-version") != "py310":
		violations.append("Ruff target-version must match the Python 3.10 application baseline")

	package = json.loads((root / "package.json").read_text(encoding="utf-8"))
	dev_dependencies = package.get("devDependencies", {})
	if set(dev_dependencies) != REQUIRED_NODE_DEV_DEPENDENCIES:
		violations.append(
			"package.json devDependencies must be exactly "
			+ ", ".join(sorted(REQUIRED_NODE_DEV_DEPENDENCIES))
		)
	for dependency, version in dev_dependencies.items():
		if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-.][0-9A-Za-z]+)*", str(version)):
			violations.append(f"quality dependency must use an exact version: {dependency}={version}")
	scripts = package.get("scripts", {})
	missing_scripts = sorted(REQUIRED_NODE_SCRIPTS - set(scripts))
	if missing_scripts:
		violations.append("package.json is missing scripts: " + ", ".join(missing_scripts))
	if scripts.get("quality") != "python scripts/quality.py":
		violations.append("package.json quality must invoke scripts/quality.py")
	if not re.fullmatch(r"npm@\d+\.\d+\.\d+", str(package.get("packageManager", ""))):
		violations.append("packageManager must pin an exact npm version")

	lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
	locked_root = lock.get("packages", {}).get("", {})
	if locked_root.get("devDependencies") != dev_dependencies:
		violations.append("package-lock.json root devDependencies differ from package.json")

	pyright = json.loads((root / "pyrightconfig.json").read_text(encoding="utf-8"))
	if not {"medops", "scripts", "tests"}.issubset(set(pyright.get("include", []))):
		violations.append("Pyright must include medops, scripts and tests")
	if pyright.get("pythonVersion") != "3.10":
		violations.append("Pyright pythonVersion must match the Python 3.10 baseline")
	if pyright.get("typeCheckingMode") not in {"basic", "standard", "strict"}:
		violations.append("Pyright typeCheckingMode must not be off")

	quality_shell = (root / "scripts" / "quality.sh").read_text(encoding="utf-8")
	if "scripts/quality.py" not in quality_shell:
		violations.append("scripts/quality.sh must delegate to scripts/quality.py")
	if "command -v ruff" in quality_shell:
		violations.append("quality checks must fail instead of silently skipping missing Ruff")
	return violations


def validate_ci_pipeline(root: Path) -> list[str]:
	workflow_path = root / ".github" / "workflows" / "ci.yml"
	integration_script_path = root / "scripts" / "ci_integration.sh"
	violations: list[str] = []
	if not workflow_path.is_file():
		violations.append("missing .github/workflows/ci.yml")
	if not integration_script_path.is_file():
		violations.append("missing scripts/ci_integration.sh")
	if violations:
		return violations

	workflow_text = workflow_path.read_text(encoding="utf-8")
	try:
		# BaseLoader constructs scalar strings only and never instantiates Python objects.
		workflow = yaml.load(  # nosec B506
			workflow_text,
			Loader=yaml.BaseLoader,
		)
	except yaml.YAMLError as exc:
		return [f"invalid CI workflow YAML: {exc}"]
	if not isinstance(workflow, dict):
		return ["CI workflow must be a mapping"]

	if workflow.get("name") != "CI":
		violations.append("CI workflow name must remain CI")
	triggers = workflow.get("on")
	if not isinstance(triggers, dict):
		violations.append("CI workflow triggers must be a mapping")
	else:
		required_triggers = {"pull_request", "push", "workflow_dispatch"}
		if not required_triggers.issubset(triggers):
			violations.append("CI must run for pull requests, main pushes and manual dispatch")
		if "pull_request_target" in triggers:
			violations.append("CI must not use pull_request_target")
		push = triggers.get("push")
		if not isinstance(push, dict) or push.get("branches") != ["main"]:
			violations.append("CI push trigger must be restricted to main")

	permissions = workflow.get("permissions")
	if permissions != {"contents": "read"}:
		violations.append("CI workflow permissions must be contents: read only")
	if "secrets." in workflow_text:
		violations.append("CI must not consume repository secrets")

	concurrency = workflow.get("concurrency")
	if not isinstance(concurrency, dict) or concurrency.get("cancel-in-progress") != "true":
		violations.append("CI must cancel superseded runs")

	jobs = workflow.get("jobs")
	if not isinstance(jobs, dict):
		return [*violations, "CI workflow jobs must be a mapping"]
	missing_jobs = sorted(REQUIRED_CI_JOBS - set(jobs))
	if missing_jobs:
		violations.append("CI workflow is missing jobs: " + ", ".join(missing_jobs))
		return violations

	expected_names = {
		"quality": "Quality",
		"security": "Security",
		"integration": "Integration",
		"required": "Required",
	}
	for job_name, display_name in expected_names.items():
		job = jobs.get(job_name)
		if not isinstance(job, dict) or job.get("name") != display_name:
			violations.append(f"CI job {job_name} must keep stable name {display_name}")
			continue
		timeout = job.get("timeout-minutes")
		if not isinstance(timeout, str) or not timeout.isdigit() or int(timeout) <= 0:
			violations.append(f"CI job {job_name} must define a positive timeout")

	integration = jobs["integration"]
	if isinstance(integration, dict):
		if integration.get("needs") != "quality":
			violations.append("CI integration job must depend on quality")
		services = integration.get("services")
		mariadb = services.get("mariadb") if isinstance(services, dict) else None
		if not isinstance(mariadb, dict) or mariadb.get("image") != "mariadb:11.8":
			violations.append("CI integration job must use MariaDB 11.8")

	security = jobs["security"]
	if isinstance(security, dict) and security.get("needs") != "quality":
		violations.append("CI security job must depend on quality")

	required = jobs["required"]
	if isinstance(required, dict):
		if set(required.get("needs", [])) != {"quality", "security", "integration"}:
			violations.append("CI required job must aggregate quality, security and integration")
		if required.get("if") != "always()":
			violations.append("CI required job must run with always()")

	action_pattern = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
	actions: list[str] = []
	for job_name, job in jobs.items():
		if not isinstance(job, dict):
			continue
		for step in job.get("steps", []):
			if not isinstance(step, dict):
				continue
			action = step.get("uses")
			if action:
				actions.append(str(action))
			if action and not str(action).startswith("./") and not action_pattern.fullmatch(str(action)):
				violations.append(f"CI action must pin a full commit SHA: {job_name}: {action}")
	quality = jobs["quality"]
	quality_commands = "\n".join(
		str(step.get("run", "")) for step in quality.get("steps", []) if isinstance(step, dict)
	)
	for required_command in (
		'pip install --disable-pip-version-check -e ".[dev]"',
		"npm ci",
		"python scripts/quality.py",
		"k6-v${K6_VERSION}-linux-amd64.tar.gz",
		"sha256sum --check",
		'" inspect \\',
		"medops/load_tests/performance_baseline.js",
		"python scripts/change_manager.py",
	):
		if required_command not in quality_commands:
			violations.append(f"CI quality job is missing command: {required_command}")
	security_commands = "\n".join(
		str(step.get("run", "")) for step in security.get("steps", []) if isinstance(step, dict)
	)
	for required_command in (
		"git fetch --force --prune --tags --unshallow",
		"gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz",
		"grype_${GRYPE_VERSION}_linux_amd64.tar.gz",
		"cyclonedx-linux-x64",
		"sha256sum --check",
		"python scripts/security_supply_chain.py run",
		'--source-commit "$GITHUB_SHA"',
	):
		if required_command not in security_commands:
			violations.append(f"CI security job is missing command: {required_command}")
	workflow_environment = workflow.get("env")
	if not isinstance(workflow_environment, dict):
		violations.append("CI workflow must pin its external tools")
	else:
		if workflow_environment.get("K6_VERSION") != "2.1.0":
			violations.append("CI must pin k6 version 2.1.0")
		if (
			workflow_environment.get("K6_LINUX_AMD64_SHA256")
			!= "295d961ebfca306f295f1133068dcd403a8171c87f387928f5f30b0fbcff858a"
		):
			violations.append("CI must pin the official k6 Linux archive SHA-256")
		expected_security_environment = {
			"GITLEAKS_VERSION": "8.30.1",
			"GITLEAKS_LINUX_X64_SHA256": ("551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"),
			"GRYPE_VERSION": "0.116.1",
			"GRYPE_LINUX_AMD64_SHA256": ("0122df7b655981abe547ad3d2190d65551dac6a2bfc80b4dc2a989b5d0587458"),
			"CYCLONEDX_CLI_VERSION": "0.33.1",
			"CYCLONEDX_CLI_LINUX_X64_SHA256": (
				"bfc8b2538da86fe239bc53658bbb63c1c8c510a293c1e6891aa5bea5d3c58746"
			),
		}
		for key, expected in expected_security_environment.items():
			if workflow_environment.get(key) != expected:
				violations.append(f"CI must pin {key} to the governed security policy")

	integration_commands = "\n".join(
		str(step.get("run", "")) for step in integration.get("steps", []) if isinstance(step, dict)
	)
	if "bash scripts/ci_integration.sh" not in integration_commands:
		violations.append("CI integration job must invoke scripts/ci_integration.sh")

	integration_script = integration_script_path.read_text(encoding="utf-8")
	for required_token in (
		"bootstrap_latest_develop.sh",
		"KEEP_TEMPORARY_REDIS=1",
		"version_lock.py",
		"migrate --skip-search-index",
		"run-tests --app medops",
		"SELECT COUNT(*) FROM `tabError Log`",
		"git -C",
	):
		if required_token not in integration_script:
			violations.append(f"CI integration script is missing: {required_token}")
	bootstrap_path = root / "scripts" / "bootstrap_latest_develop.sh"
	if not bootstrap_path.is_file():
		violations.append("missing scripts/bootstrap_latest_develop.sh")
	else:
		bootstrap = bootstrap_path.read_text(encoding="utf-8")
		for required_token in (
			'if [[ -e "$BENCH_DIR" ]]',
			'git -C "$app_dir" reset --hard HEAD',
			'git -C "$app_dir" clean -fd',
			'git -C "$app_dir" checkout --detach "$commit"',
		):
			if required_token not in bootstrap:
				violations.append(f"locked Bench bootstrap is missing: {required_token}")
	return violations


def validate_version_baseline(root: Path) -> list[str]:
	violations: list[str] = []
	try:
		lock = load_lock(root / "resolved_versions.lock.json")
	except ValueError as exc:
		return [f"invalid resolved_versions.lock.json: {exc}"]

	baseline_path = root / "architecture" / "version_baseline.json"
	if not baseline_path.is_file():
		return ["missing architecture/version_baseline.json"]
	baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
	if baseline.get("lock_file") != "../resolved_versions.lock.json":
		violations.append("version baseline must reference resolved_versions.lock.json")
	rows = baseline.get("repositories")
	if not isinstance(rows, dict):
		return [*violations, "version baseline repositories must be an object"]
	for app in UPSTREAM_APPS:
		baseline_row = rows.get(app)
		if not isinstance(baseline_row, dict):
			violations.append(f"version baseline is missing {app}")
			continue
		for field in ("url", "branch", "version_marker", "commit"):
			lock_field = "repository" if field == "url" else "version" if field == "version_marker" else field
			if baseline_row.get(field) != lock["apps"][app].get(lock_field):
				violations.append(f"version baseline {app}.{field} differs from lock")
	return violations


def validate_catalog_ownership(root: Path) -> list[str]:
	violations: list[str] = []
	csv_catalogs = (
		root / "design" / "doctype_catalog.csv",
		root / "design" / "field_catalog.csv",
		root / "api" / "api_catalog.csv",
		root / "backlog" / "backlog.csv",
	)
	for path in csv_catalogs:
		with path.open(encoding="utf-8-sig", newline="") as handle:
			apps = {row.get("app", "") for row in csv.DictReader(handle)}
		if apps != {APP_NAME}:
			violations.append(f"{path.relative_to(root)} app values are {sorted(apps)}")

	for path in (root / "doctype_blueprints").rglob("*.json"):
		payload = json.loads(path.read_text(encoding="utf-8"))
		if payload.get("x_hrp", {}).get("app") != APP_NAME:
			violations.append(f"{path.relative_to(root)} has wrong x_hrp.app")
	return violations


def validate_module_structure(root: Path) -> list[str]:
	return validate_module_source_tree(root, expected_module_count=36)


def validate_module_boundaries(root: Path) -> list[str]:
	"""Require cross-module Python imports to use the target module's services facade."""
	package_root = root / APP_NAME
	violations: list[str] = []
	if not package_root.is_dir():
		return [f"missing {APP_NAME} package directory"]
	module_pattern = re.compile(r"^medops\.(hrp_[a-z0-9_]+)(?:\.(.+))?$")
	for source_root in package_root.iterdir():
		if not source_root.is_dir() or not source_root.name.startswith("hrp_"):
			continue
		for path in source_root.rglob("*.py"):
			try:
				tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
			except SyntaxError:
				continue
			imports: list[str] = []
			for node in ast.walk(tree):
				if isinstance(node, ast.ImportFrom) and node.module:
					imports.append(node.module)
				elif isinstance(node, ast.Import):
					imports.extend(alias.name for alias in node.names)
			for imported in imports:
				match = module_pattern.match(imported)
				if not match:
					continue
				target_package, target_path = match.groups()
				if target_package == source_root.name:
					continue
				if target_path and target_path.split(".", 1)[0] == "services":
					continue
				violations.append(
					f"{path.relative_to(root)} imports private cross-module path {imported}; "
					f"use medops.{target_package}.services"
				)
	return violations


def validate_protected_ledgers(root: Path) -> list[str]:
	app_root = root / APP_NAME
	violations: list[str] = []
	for path in app_root.rglob("*.py"):
		text = path.read_text(encoding="utf-8")
		for pattern in PROTECTED_LEDGER_PATTERNS:
			if pattern.search(text):
				violations.append(
					f"{path.relative_to(root)} contains forbidden protected-ledger write pattern"
				)
				break
	return violations


def find_direct_frappe_throws(root: Path) -> list[str]:
	"""Keep one application-level adapter for Frappe error and permission helpers."""
	app_root = root / APP_NAME
	allowed_path = app_root / "services" / "errors.py"
	forbidden_helpers = frozenset({"only_for", "throw"})
	violations: list[str] = []
	if not app_root.is_dir():
		return [f"missing {APP_NAME} package directory"]
	for path in app_root.rglob("*.py"):
		if path == allowed_path:
			continue
		try:
			tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
		except SyntaxError:
			continue
		imported_helpers = {
			alias.asname or alias.name: alias.name
			for node in ast.walk(tree)
			if isinstance(node, ast.ImportFrom) and node.module == "frappe"
			for alias in node.names
			if alias.name in forbidden_helpers
		}
		for node in ast.walk(tree):
			if not isinstance(node, ast.Call):
				continue
			helper_name: str | None = None
			if (
				isinstance(node.func, ast.Attribute)
				and isinstance(node.func.value, ast.Name)
				and node.func.value.id == "frappe"
				and node.func.attr in forbidden_helpers
			):
				helper_name = node.func.attr
			elif isinstance(node.func, ast.Name):
				helper_name = imported_helpers.get(node.func.id)
			if helper_name is not None:
				violations.append(
					f"{path.relative_to(root)}:{node.lineno} calls frappe.{helper_name} outside "
					"medops.services.errors"
				)
	return violations


def validate_error_contract(root: Path) -> list[str]:
	catalog_path = root / APP_NAME / "config" / "error_catalog.json"
	translation_path = root / APP_NAME / "translations" / "zh.csv"
	common_path = root / APP_NAME / "common" / "error_catalog.py"
	service_path = root / APP_NAME / "services" / "errors.py"
	api_path = root / APP_NAME / "api" / "v1" / "errors.py"
	documentation_path = root / "architecture" / "errors.md"
	task_path = root / "backlog" / "COD-009.md"
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		catalog_path,
		translation_path,
		common_path,
		service_path,
		api_path,
		documentation_path,
		task_path,
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing error contract file: {path}" for path in missing]

	try:
		catalog = load_error_catalog(catalog_path)
		validate_error_translations(catalog, translation_path)
	except ErrorCatalogError as exc:
		return [f"invalid error contract: {exc}"]

	violations = find_direct_frappe_throws(root)
	actual_baseline = {key: catalog.get(key).code for key in BASELINE_ERROR_CODES if key in catalog.by_key}
	if actual_baseline != BASELINE_ERROR_CODES:
		violations.append("error catalog must preserve all baseline IONE-CORE error codes")

	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		'"ione_error"',
		'"X-Ione-Error-Code"',
		'"X-Ione-Error-ID"',
		'"medops.errors"',
		"http_write_enabled",
	):
		if token not in service_text:
			violations.append(f"error service is missing contract token: {token}")

	error_api = "/api/method/medops.api.v1.errors.get_error_catalog"
	for catalog_file in catalog_paths:
		if error_api not in catalog_file.read_text(encoding="utf-8"):
			violations.append(f"{catalog_file.relative_to(root)} is missing the error catalog endpoint")
	api_text = api_path.read_text(encoding="utf-8")
	if '@frappe.whitelist(allow_guest=True, methods=["GET"])' not in api_text:
		violations.append("error catalog endpoint must enter the application layer for uniform guest errors")
	if "IoneError" not in (root / "api" / "openapi.yaml").read_text(encoding="utf-8"):
		violations.append("OpenAPI must define the IoneError response schema")
	return violations


def find_direct_audit_loggers(root: Path) -> list[str]:
	"""Require application audit events to pass through the redacting context service."""
	app_root = root / APP_NAME
	allowed_path = app_root / "services" / "audit_context.py"
	violations: list[str] = []
	for path in app_root.rglob("*.py"):
		if path == allowed_path or "tests" in path.parts:
			continue
		try:
			tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
		except SyntaxError:
			continue
		for node in ast.walk(tree):
			if (
				isinstance(node, ast.Call)
				and isinstance(node.func, ast.Attribute)
				and isinstance(node.func.value, ast.Name)
				and node.func.value.id == "frappe"
				and node.func.attr == "logger"
			):
				violations.append(
					f"{path.relative_to(root)}:{node.lineno} calls frappe.logger outside "
					"medops.services.audit_context"
				)
	return violations


def find_direct_transaction_commits(root: Path) -> list[str]:
	"""Keep the final transaction commit owned by the Frappe request or job."""
	app_root = root / APP_NAME
	violations: list[str] = []
	if not app_root.is_dir():
		return [f"missing {APP_NAME} package directory"]
	for path in app_root.rglob("*.py"):
		try:
			tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
		except SyntaxError:
			continue
		for node in ast.walk(tree):
			if (
				isinstance(node, ast.Call)
				and isinstance(node.func, ast.Attribute)
				and node.func.attr == "commit"
				and isinstance(node.func.value, ast.Attribute)
				and node.func.value.attr == "db"
				and isinstance(node.func.value.value, ast.Name)
				and node.func.value.value.id == "frappe"
			):
				violations.append(
					f"{path.relative_to(root)}:{node.lineno} calls frappe.db.commit; "
					"the outer Frappe transaction owns the commit"
				)
	return violations


def validate_audit_context_contract(root: Path) -> list[str]:
	common_path = root / APP_NAME / "common" / "audit_context.py"
	service_path = root / APP_NAME / "services" / "audit_context.py"
	api_path = root / APP_NAME / "api" / "v1" / "audit.py"
	hooks_path = root / APP_NAME / "hooks.py"
	documentation_path = root / "architecture" / "audit_context.md"
	task_path = root / "backlog" / "COD-010.md"
	test_paths = (
		root / "tests" / "test_audit_context.py",
		root / APP_NAME / "hrp_foundation" / "tests" / "test_audit_context.py",
	)
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		common_path,
		service_path,
		api_path,
		hooks_path,
		documentation_path,
		task_path,
		*test_paths,
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing audit context contract file: {path}" for path in missing]

	violations = find_direct_audit_loggers(root)
	hooks_text = hooks_path.read_text(encoding="utf-8")
	for token in (
		"start_http_audit_context",
		"finish_http_audit_context",
		"start_job_audit_context",
		"finish_job_audit_context",
	):
		if token not in hooks_text:
			violations.append(f"Frappe hooks are missing audit lifecycle token: {token}")

	common_text = common_path.read_text(encoding="utf-8")
	for token in (
		"AUDIT_CONTEXT_SCHEMA_VERSION = 1",
		"class AuditContext",
		"as_propagation_dict",
		"parse_propagation_payload",
		"FORBIDDEN_AUDIT_FIELD_MARKERS",
	):
		if token not in common_text:
			violations.append(f"audit context model is missing: {token}")

	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		'"X-Correlation-ID"',
		'"X-Request-ID"',
		"AUDIT_CONTEXT_JOB_KWARG",
		"kwargs.pop(AUDIT_CONTEXT_JOB_KWARG",
		"emit_audit_event",
		"enqueue_with_audit",
		"http_write_enabled",
	):
		if token not in service_text:
			violations.append(f"audit context service is missing: {token}")

	api_text = api_path.read_text(encoding="utf-8")
	if '@frappe.whitelist(allow_guest=True, methods=["GET"])' not in api_text:
		violations.append("audit context endpoint must enter the application layer for uniform guest errors")

	audit_api = "/api/method/medops.api.v1.audit.get_audit_context"
	for catalog_path in catalog_paths:
		if audit_api not in catalog_path.read_text(encoding="utf-8"):
			violations.append(f"{catalog_path.relative_to(root)} is missing the audit context endpoint")

	openapi_text = (root / "api" / "openapi.yaml").read_text(encoding="utf-8")
	for token in ("X-Correlation-ID", "X-Request-ID", "AuditContext", "correlation_id", "request_id"):
		if token not in openapi_text:
			violations.append(f"OpenAPI is missing audit context token: {token}")

	for script_name in ("change_manager.py", "environment_manager.py", "fixture_manager.py"):
		script_text = (root / "scripts" / script_name).read_text(encoding="utf-8")
		if "medops.common.audit_context" not in script_text:
			violations.append(f"{script_name} must use the shared correlation ID contract")
		if "CORRELATION_ID_PATTERN" in script_text:
			violations.append(f"{script_name} must not define a duplicate correlation ID pattern")

	for path in (root / APP_NAME).rglob("*.py"):
		if path == service_path or "tests" in path.parts:
			continue
		text = path.read_text(encoding="utf-8")
		if "X-Correlation-ID" in text:
			violations.append(
				f"{path.relative_to(root)} reads X-Correlation-ID outside the audit context service"
			)
		if "CORRELATION_ID_PATTERN" in text:
			violations.append(f"{path.relative_to(root)} defines a duplicate correlation ID pattern")
	return violations


def validate_domain_service_contract(root: Path) -> list[str]:
	common_path = root / APP_NAME / "common" / "domain_service.py"
	service_path = root / APP_NAME / "services" / "domain_service.py"
	idempotency_path = root / APP_NAME / "services" / "idempotency.py"
	doctype_root = root / APP_NAME / "hrp_foundation" / "doctype" / "hrp_service_idempotency"
	doctype_json_path = doctype_root / "hrp_service_idempotency.json"
	doctype_controller_path = doctype_root / "hrp_service_idempotency.py"
	facade_path = root / APP_NAME / "hrp_foundation" / "services" / "__init__.py"
	example_service_path = root / APP_NAME / "hrp_foundation" / "services" / "module_settings.py"
	api_path = root / APP_NAME / "api" / "v1" / "modules.py"
	documentation_path = root / "architecture" / "domain_services.md"
	adr_path = root / "architecture" / "adr" / "ADR-0006-domain-service-and-durable-idempotency.md"
	task_path = root / "backlog" / "COD-011.md"
	change_path = root / "changes" / "COD-011.json"
	test_paths = (
		root / "tests" / "test_domain_service.py",
		root / APP_NAME / "hrp_foundation" / "tests" / "test_domain_service.py",
		root / APP_NAME / "hrp_foundation" / "tests" / "test_modules.py",
	)
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		common_path,
		service_path,
		idempotency_path,
		doctype_json_path,
		doctype_controller_path,
		facade_path,
		example_service_path,
		api_path,
		documentation_path,
		adr_path,
		task_path,
		change_path,
		*test_paths,
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing domain service contract file: {path}" for path in missing]

	violations = find_direct_transaction_commits(root)

	common_text = common_path.read_text(encoding="utf-8")
	for token in (
		"class DomainServiceDefinition",
		"class DomainServiceExecution",
		"canonical_json_object",
		"idempotency_key_hash",
		"idempotency_record_name",
		"MAX_JSON_SNAPSHOT_BYTES",
	):
		if token not in common_text:
			violations.append(f"domain service model is missing: {token}")

	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		"class DomainService",
		"require_roles",
		"frappe.db.savepoint",
		"frappe.db.rollback",
		"reserve_idempotency",
		"complete_idempotency",
		"domain_service_started",
		"domain_service_completed",
		"domain_service_replayed",
		"domain_service_failed",
	):
		if token not in service_text:
			violations.append(f"domain service template is missing: {token}")

	idempotency_text = idempotency_path.read_text(encoding="utf-8")
	for token in (
		'request.headers.get("Idempotency-Key")',
		"idempotency_key_hash",
		"request_fingerprint",
		"response_fingerprint",
		"encrypt(serialized)",
		"decrypt(record.response_snapshot)",
		'raise_ione_error("IDEMPOTENCY_CONFLICT")',
	):
		if token not in idempotency_text:
			violations.append(f"idempotency service is missing: {token}")

	doctype_payload = json.loads(doctype_json_path.read_text(encoding="utf-8"))
	if doctype_payload.get("module") != "HRP Foundation":
		violations.append("HRP Service Idempotency must belong to HRP Foundation")
	fields = {
		str(field.get("fieldname")): field
		for field in doctype_payload.get("fields", [])
		if isinstance(field, dict)
	}
	for forbidden_field in ("idempotency_key", "request_payload", "request_snapshot"):
		if forbidden_field in fields:
			violations.append(f"HRP Service Idempotency must not persist plaintext field {forbidden_field}")
	response_snapshot = fields.get("response_snapshot", {})
	if (
		response_snapshot.get("fieldtype") != "Long Text"
		or response_snapshot.get("hidden") != 1
		or response_snapshot.get("read_only") != 1
		or response_snapshot.get("no_copy") != 1
	):
		violations.append("encrypted response_snapshot must be hidden, read-only and non-copyable")
	allowed_permission_keys = {"role", "read", "report", "select"}
	for permission in doctype_payload.get("permissions", []):
		if not isinstance(permission, dict):
			violations.append("HRP Service Idempotency contains an invalid permission row")
			continue
		if permission.get("role") not in {"System Manager", "HRP System Manager"}:
			violations.append("HRP Service Idempotency has an unauthorized role")
		if any(key not in allowed_permission_keys and bool(value) for key, value in permission.items()):
			violations.append("HRP Service Idempotency permissions must be read-only")

	facade_text = facade_path.read_text(encoding="utf-8")
	if "set_module_enabled" not in facade_text:
		violations.append("HRP Foundation facade must expose the module command")
	api_text = api_path.read_text(encoding="utf-8")
	if "set_module_enabled_service" not in api_text:
		violations.append("module write API must delegate to the HRP Foundation service facade")
	if ".save(" in api_text:
		violations.append("module write API must not save DocTypes directly")
	api_tree = ast.parse(api_text, filename=str(api_path))
	module_write_functions = [
		node
		for node in api_tree.body
		if isinstance(node, ast.FunctionDef) and node.name == "set_module_enabled"
	]
	if len(module_write_functions) != 1:
		violations.append("module write API must define exactly one set_module_enabled endpoint")
	elif "idempotency_key" in {argument.arg for argument in module_write_functions[0].args.args}:
		violations.append("HTTP idempotency keys must be accepted only through the request header")

	module_api = "/api/method/medops.api.v1.modules.set_module_enabled"
	for catalog_path in catalog_paths:
		catalog_text = catalog_path.read_text(encoding="utf-8")
		if module_api not in catalog_text:
			violations.append(
				f"{catalog_path.relative_to(root)} is missing the domain service example endpoint"
			)
		if "Idempotency-Key" not in catalog_text:
			violations.append(f"{catalog_path.relative_to(root)} is missing the idempotency header contract")

	openapi_payload = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	operation = openapi_payload.get("paths", {}).get(module_api, {}).get("post", {})
	idempotency_headers = [
		parameter
		for parameter in operation.get("parameters", [])
		if isinstance(parameter, dict)
		and parameter.get("in") == "header"
		and parameter.get("name") == "Idempotency-Key"
	]
	if len(idempotency_headers) != 1 or idempotency_headers[0].get("required") is not True:
		violations.append("PLT-009 OpenAPI must require exactly one Idempotency-Key header")
	if operation.get("x-idempotency") != "Required for write; encrypted response snapshot":
		violations.append("PLT-009 OpenAPI must declare durable encrypted idempotency")
	return violations


def find_direct_immutable_ledger_bypasses(root: Path) -> list[str]:
	"""Keep concrete HRP ledger access behind the shared immutable-ledger service."""
	app_root = root / APP_NAME
	allowed_path = app_root / "services" / "immutable_ledger.py"
	ledger_pattern = re.compile(r"^HRP [A-Za-z0-9][A-Za-z0-9 -]{1,134} Ledger$")
	forbidden_calls = frozenset(
		{
			"frappe.delete_doc",
			"frappe.get_doc",
			"frappe.new_doc",
			"frappe.rename_doc",
			"frappe.db.bulk_insert",
			"frappe.db.bulk_update",
			"frappe.db.delete",
			"frappe.db.set_value",
			"frappe.db.sql",
		}
	)
	violations: list[str] = []

	def attribute_path(node: ast.AST) -> str | None:
		parts: list[str] = []
		while isinstance(node, ast.Attribute):
			parts.append(node.attr)
			node = node.value
		if not isinstance(node, ast.Name):
			return None
		return ".".join((node.id, *reversed(parts)))

	for path in app_root.rglob("*.py"):
		if path == allowed_path or "tests" in path.parts:
			continue
		try:
			tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
		except SyntaxError:
			continue
		for node in ast.walk(tree):
			if not isinstance(node, ast.Call) or attribute_path(node.func) not in forbidden_calls:
				continue
			literals = [
				constant.value
				for constant in ast.walk(node)
				if isinstance(constant, ast.Constant) and isinstance(constant.value, str)
			]
			if any(ledger_pattern.fullmatch(value) or f"tab{value}" in value for value in literals):
				violations.append(
					f"{path.relative_to(root)}:{node.lineno} bypasses the immutable-ledger service"
				)
	return violations


def validate_immutable_ledger_contract(root: Path) -> list[str]:
	common_path = root / APP_NAME / "common" / "immutable_ledger.py"
	service_path = root / APP_NAME / "services" / "immutable_ledger.py"
	api_path = root / APP_NAME / "api" / "v1" / "ledgers.py"
	documentation_path = root / "architecture" / "immutable_ledgers.md"
	adr_path = root / "architecture" / "adr" / "ADR-0007-immutable-ledger-base.md"
	task_path = root / "backlog" / "COD-012.md"
	change_path = root / "changes" / "COD-012.json"
	test_paths = (
		root / "tests" / "test_immutable_ledger.py",
		root / APP_NAME / "hrp_foundation" / "tests" / "test_immutable_ledger.py",
	)
	blueprint_paths = (
		root / "doctype_blueprints" / "hrp_budget" / "hrp_budget_ledger.json",
		root / "doctype_blueprints" / "hrp_inventory_spd" / "hrp_department_stock_ledger.json",
	)
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		common_path,
		service_path,
		api_path,
		documentation_path,
		adr_path,
		task_path,
		change_path,
		*test_paths,
		*blueprint_paths,
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing immutable ledger contract file: {path}" for path in missing]

	violations = find_direct_immutable_ledger_bypasses(root)
	common_text = common_path.read_text(encoding="utf-8")
	for token in (
		"class ImmutableLedgerDefinition",
		"BASE_LEDGER_FIELDS",
		"build_reversal_values",
		"assert_reversal_matches",
		"one_reversal_per_entry",
		'"http_write_enabled": False',
	):
		if token not in common_text:
			violations.append(f"immutable ledger model is missing: {token}")

	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		"class ImmutableLedgerDocument",
		"class AppendImmutableLedgerService",
		"class ReverseImmutableLedgerService",
		"for_update=True",
		"wait=False",
		"immutable_ledger_appended",
		"immutable_ledger_reversed",
		'raise_ione_error("OPERATION_NOT_ALLOWED")',
	):
		if token not in service_text:
			violations.append(f"immutable ledger service is missing: {token}")
	if "frappe.db.commit" in service_text:
		violations.append("immutable ledger service must not own the transaction commit")

	expected_fields = {field.fieldname: (field.fieldtype, field.required) for field in BASE_LEDGER_FIELDS}
	for blueprint_path in blueprint_paths:
		payload = json.loads(blueprint_path.read_text(encoding="utf-8"))
		fields = {
			str(field.get("fieldname")): field
			for field in payload.get("fields", [])
			if isinstance(field, dict)
		}
		for fieldname, (fieldtype, required) in expected_fields.items():
			field = fields.get(fieldname)
			if field is None or field.get("fieldtype") != fieldtype or (required and field.get("reqd") != 1):
				violations.append(
					f"{blueprint_path.relative_to(root)} violates base ledger field {fieldname}"
				)
		if payload.get("is_submittable") != 0 or payload.get("allow_rename") != 0:
			violations.append(f"{blueprint_path.relative_to(root)} must be append-only")
		for permission in payload.get("permissions", []):
			if any(
				bool(permission.get(permission_name))
				for permission_name in IMMUTABLE_LEDGER_FORBIDDEN_PERMISSIONS
				if permission_name in permission
			):
				violations.append(f"{blueprint_path.relative_to(root)} contains a ledger write permission")

	ledger_api = "/api/method/medops.api.v1.ledgers.get_immutable_ledger_contract"
	for catalog_path in catalog_paths:
		if ledger_api not in catalog_path.read_text(encoding="utf-8"):
			violations.append(f"{catalog_path.relative_to(root)} is missing the immutable ledger endpoint")
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	operation = openapi.get("paths", {}).get(ledger_api, {}).get("get", {})
	if operation.get("x-transaction-boundary") != "Read-only":
		violations.append("PLT-015 immutable ledger contract API must be read-only")
	if operation.get("x-required-role") != "System Manager or HRP System Manager":
		violations.append("PLT-015 immutable ledger contract API has the wrong role contract")
	return violations


def find_direct_transactional_message_bypasses(root: Path) -> list[str]:
	"""Keep concrete HRP Outbox and Inbox access behind the shared message service."""
	app_root = root / APP_NAME
	allowed_path = app_root / "services" / "transactional_message.py"
	message_pattern = re.compile(r"^HRP [A-Za-z0-9][A-Za-z0-9 -]{1,126} (?:Outbox|Inbox)$")
	forbidden_calls = frozenset(
		{
			"frappe.delete_doc",
			"frappe.get_doc",
			"frappe.new_doc",
			"frappe.rename_doc",
			"frappe.db.bulk_insert",
			"frappe.db.bulk_update",
			"frappe.db.delete",
			"frappe.db.set_value",
			"frappe.db.sql",
		}
	)
	violations: list[str] = []

	def attribute_path(node: ast.AST) -> str | None:
		parts: list[str] = []
		while isinstance(node, ast.Attribute):
			parts.append(node.attr)
			node = node.value
		if not isinstance(node, ast.Name):
			return None
		return ".".join((node.id, *reversed(parts)))

	if not app_root.is_dir():
		return [f"missing {APP_NAME} package directory"]
	for path in app_root.rglob("*.py"):
		if path == allowed_path or "tests" in path.parts:
			continue
		try:
			tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
		except SyntaxError:
			continue
		for node in ast.walk(tree):
			if not isinstance(node, ast.Call) or attribute_path(node.func) not in forbidden_calls:
				continue
			literals = [
				constant.value
				for constant in ast.walk(node)
				if isinstance(constant, ast.Constant) and isinstance(constant.value, str)
			]
			if any(message_pattern.fullmatch(value) or f"tab{value}" in value for value in literals):
				violations.append(
					f"{path.relative_to(root)}:{node.lineno} bypasses the transactional-message service"
				)
	return violations


def validate_transactional_message_contract(root: Path) -> list[str]:
	common_path = root / APP_NAME / "common" / "transactional_message.py"
	service_path = root / APP_NAME / "services" / "transactional_message.py"
	api_path = root / APP_NAME / "api" / "v1" / "messages.py"
	documentation_path = root / "architecture" / "transactional_messages.md"
	adr_path = root / "architecture" / "adr" / "ADR-0008-transactional-outbox-inbox.md"
	task_path = root / "backlog" / "COD-013.md"
	change_path = root / "changes" / "COD-013.json"
	test_paths = (
		root / "tests" / "test_transactional_message.py",
		root / APP_NAME / "hrp_integration" / "tests" / "test_transactional_message.py",
	)
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		common_path,
		service_path,
		api_path,
		documentation_path,
		adr_path,
		task_path,
		change_path,
		*test_paths,
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing transactional message contract file: {path}" for path in missing]

	violations = find_direct_transactional_message_bypasses(root)
	common_text = common_path.read_text(encoding="utf-8")
	for token in (
		"class MessageBoxDefinition",
		"BASE_MESSAGE_FIELDS",
		"normalize_message_snapshot",
		"message_record_name",
		"processing_token_matches",
		'"outbox_delivery": "at_least_once"',
		'"inbox_deduplication": "consumer_and_event_id"',
		'"http_write_enabled": False',
	):
		if token not in common_text:
			violations.append(f"transactional message model is missing: {token}")
	if len(BASE_MESSAGE_FIELDS) != 23:
		violations.append("transactional message base must define exactly 23 fields")

	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		"class TransactionalMessageDocument",
		"class PublishOutboxService",
		"class ClaimOutboxService",
		"class CompleteOutboxService",
		"class FailOutboxService",
		"class BeginInboxService",
		"class CompleteInboxService",
		"class FailInboxService",
		"for_update=True",
		"wait=False",
		"processing_token_hash",
		"transactional_outbox_published",
		"transactional_inbox_started",
		'raise_ione_error("OPERATION_NOT_ALLOWED")',
	):
		if token not in service_text:
			violations.append(f"transactional message service is missing: {token}")
	if "frappe.db.commit" in service_text:
		violations.append("transactional message service must not own the transaction commit")
	try:
		service_tree = ast.parse(service_text, filename=str(service_path))
	except SyntaxError as exc:
		violations.append(f"transactional message service is invalid Python: {exc}")
	else:
		network_roots = {"aiohttp", "httpx", "requests", "socket", "urllib"}
		imported_roots: set[str] = set()
		for node in ast.walk(service_tree):
			if isinstance(node, ast.Import):
				imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
			elif isinstance(node, ast.ImportFrom) and node.module:
				imported_roots.add(node.module.split(".", 1)[0])
		if imported_roots.intersection(network_roots):
			violations.append("transactional message base must not import a network client")

	message_api = "/api/method/medops.api.v1.messages.get_transactional_message_contract"
	for catalog_path in catalog_paths:
		if message_api not in catalog_path.read_text(encoding="utf-8"):
			violations.append(
				f"{catalog_path.relative_to(root)} is missing the transactional message endpoint"
			)
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	operation = openapi.get("paths", {}).get(message_api, {}).get("get", {})
	if operation.get("x-transaction-boundary") != "Read-only":
		violations.append("PLT-016 transactional message contract API must be read-only")
	if operation.get("x-required-role") != "System Manager or HRP System Manager":
		violations.append("PLT-016 transactional message contract API has the wrong role contract")
	if "post" in openapi.get("paths", {}).get(message_api, {}):
		violations.append("PLT-016 transactional message contract must not expose writes")
	return violations


def validate_test_data_factory_contract(root: Path) -> list[str]:
	config_path = root / APP_NAME / "config" / "test_data_scenarios.json"
	common_path = root / APP_NAME / "common" / "test_data_factory.py"
	service_path = root / APP_NAME / "services" / "test_data_factory.py"
	facade_path = root / APP_NAME / "hrp_foundation" / "services" / "test_data.py"
	api_path = root / APP_NAME / "api" / "v1" / "test_data.py"
	documentation_path = root / "architecture" / "test_data_factory.md"
	adr_path = root / "architecture" / "adr" / "ADR-0009-source-controlled-test-data-factory.md"
	task_path = root / "backlog" / "COD-014.md"
	change_path = root / "changes" / "COD-014.json"
	test_paths = (
		root / "tests" / "test_test_data_factory.py",
		root / APP_NAME / "hrp_foundation" / "tests" / "test_test_data_factory.py",
	)
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		config_path,
		common_path,
		service_path,
		facade_path,
		api_path,
		documentation_path,
		adr_path,
		task_path,
		change_path,
		*test_paths,
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing test data factory contract file: {path}" for path in missing]

	violations: list[str] = []
	try:
		registry = load_test_data_scenario_registry(config_path)
	except TestDataFactoryContractError as exc:
		return [f"invalid test data scenario registry: {exc}"]
	if not registry.scenarios:
		violations.append("test data factory must declare at least one source-controlled scenario")
	for scenario in registry.scenarios:
		if set(scenario.allowed_profiles) - {"development", "test"}:
			violations.append(f"{scenario.scenario_id} permits a non-test environment")
		if scenario.contains_personal_data:
			violations.append(f"{scenario.scenario_id} permits personal data")
		scenario.ordered_steps()

	common_text = common_path.read_text(encoding="utf-8")
	for token in (
		"class TestDataScenarioDefinition",
		"class TestDataScenarioRegistry",
		"ordered_steps",
		"normalize_test_data_seed",
		"test_dataset_id",
		"synthetic_identifier",
		'"arbitrary_doctype_input": False',
		'"contains_personal_data": False',
		'"http_write_enabled": False',
	):
		if token not in common_text:
			violations.append(f"test data factory model is missing: {token}")

	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		"class GenerateTestDataService",
		"DomainServiceDefinition",
		"require_roles",
		"_assert_generation_allowed",
		"synthetic_data_only",
		"allow_tests",
		"_STEP_BUILDERS",
		"document.insert(ignore_permissions=True)",
		"test_data_factory_generated",
	):
		if token not in service_text:
			violations.append(f"test data factory service is missing: {token}")
	if "frappe.db.commit" in service_text:
		violations.append("test data factory service must not own the transaction commit")
	if "GL Entry" in service_text or "Stock Ledger Entry" in service_text or '"Bin"' in service_text:
		violations.append("test data factory must not write protected ERPNext ledgers")
	if "importlib" in common_text or "importlib" in service_text:
		violations.append("test data factory builders must not use dynamic imports")

	api_text = api_path.read_text(encoding="utf-8")
	if '@frappe.whitelist(allow_guest=True, methods=["GET"])' not in api_text:
		violations.append("PLT-017 test data factory contract must be GET-only")
	if "generate_test_data" in api_text:
		violations.append("test data generation must not be exposed through HTTP")

	factory_api = "/api/method/medops.api.v1.test_data.get_test_data_factory_contract"
	for catalog_path in catalog_paths:
		if factory_api not in catalog_path.read_text(encoding="utf-8"):
			violations.append(f"{catalog_path.relative_to(root)} is missing the test data factory endpoint")
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	path_contract = openapi.get("paths", {}).get(factory_api, {})
	operation = path_contract.get("get", {})
	if operation.get("x-transaction-boundary") != "Read-only":
		violations.append("PLT-017 test data factory contract API must be read-only")
	if operation.get("x-required-role") != "System Manager or HRP System Manager":
		violations.append("PLT-017 test data factory contract API has the wrong role contract")
	if set(path_contract) != {"get"}:
		violations.append("PLT-017 test data factory contract must not expose writes")
	return violations


def validate_performance_baseline_contract(root: Path) -> list[str]:
	config_path = root / APP_NAME / "config" / "performance_baselines.json"
	common_path = root / APP_NAME / "common" / "performance_baseline.py"
	service_path = root / APP_NAME / "services" / "performance_baseline.py"
	facade_path = root / APP_NAME / "hrp_foundation" / "services" / "performance.py"
	api_path = root / APP_NAME / "api" / "v1" / "performance.py"
	k6_path = root / APP_NAME / "load_tests" / "performance_baseline.js"
	runner_path = root / "scripts" / "performance_baseline.py"
	documentation_path = root / "architecture" / "performance_baselines.md"
	adr_path = root / "architecture" / "adr" / "ADR-0010-external-governed-performance-baseline.md"
	task_path = root / "backlog" / "COD-015.md"
	change_path = root / "changes" / "COD-015.json"
	test_paths = (
		root / "tests" / "test_performance_baseline.py",
		root / APP_NAME / "hrp_foundation" / "tests" / "test_performance_baseline.py",
	)
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		config_path,
		common_path,
		service_path,
		facade_path,
		api_path,
		k6_path,
		runner_path,
		documentation_path,
		adr_path,
		task_path,
		change_path,
		*test_paths,
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing performance baseline contract file: {path}" for path in missing]

	violations: list[str] = []
	try:
		registry = load_performance_baseline_registry(config_path)
	except PerformanceBaselineContractError as exc:
		return [f"invalid performance baseline registry: {exc}"]
	if not registry.scenarios:
		violations.append("performance baseline must declare at least one source-controlled scenario")
	if set(registry.policy.allowed_profiles) - {"development", "test"}:
		violations.append("performance baseline permits a non-test environment")
	if registry.policy.http_write_enabled:
		violations.append("performance baseline must disable HTTP writes")
	for scenario in registry.scenarios:
		if scenario.method != "GET" or not scenario.read_only:
			violations.append(f"{scenario.scenario_id} is not read-only")
		if scenario.contains_personal_data:
			violations.append(f"{scenario.scenario_id} permits personal data")
		for profile in scenario.profiles:
			if profile.virtual_users > registry.policy.max_virtual_users:
				violations.append(f"{scenario.scenario_id}/{profile.profile} exceeds the VU limit")
			if profile.iterations > registry.policy.max_iterations:
				violations.append(f"{scenario.scenario_id}/{profile.profile} exceeds the request limit")
			if profile.max_duration_seconds > registry.policy.max_duration_seconds:
				violations.append(f"{scenario.scenario_id}/{profile.profile} exceeds the duration limit")

	common_text = common_path.read_text(encoding="utf-8")
	for token in (
		"class PerformanceBaselineRegistry",
		"class PerformanceScenarioDefinition",
		"class PerformanceLoadProfile",
		"class PerformanceRunSummary",
		"evaluate_performance_run",
		'"http_write_enabled": self.http_write_enabled',
		'"external_k6_process"',
	):
		if token not in common_text:
			violations.append(f"performance baseline model is missing: {token}")

	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		"require_roles",
		"get_environment_status",
		"_is_load_test_available",
		"allow_tests",
		"synthetic_data_only",
		"public_access",
		"external_integrations_enabled",
		"performance_baseline_contract_read",
	):
		if token not in service_text:
			violations.append(f"performance baseline service is missing: {token}")
	if "frappe.db.commit" in service_text:
		violations.append("performance baseline service must not own a transaction commit")

	api_text = api_path.read_text(encoding="utf-8")
	if '@frappe.whitelist(allow_guest=True, methods=["GET"])' not in api_text:
		violations.append("PLT-018 performance baseline contract must be GET-only")
	if "run_performance" in api_text or "evaluate_performance" in api_text:
		violations.append("performance execution or result writes must not be exposed through HTTP")

	k6_text = k6_path.read_text(encoding="utf-8")
	for token in (
		"shared-iterations",
		"IONE_PERF_CONFIRM",
		"NON_PRODUCTION_LOAD_TEST",
		"get_performance_baseline_contract",
		"load_test_available === true",
		"IONE_PERF_REGISTRY_SHA256",
		"registry.policy.k6_version",
		"summaryTrendStats",
		"ione_scenario_requests",
		"ione_scenario_failed",
		"ione_scenario_check",
		"ione_scenario_duration",
	):
		if token not in k6_text:
			violations.append(f"k6 performance runner is missing: {token}")
	for forbidden in (
		"http.post",
		"http.put",
		"http.patch",
		"http.del",
		"insecureSkipTLSVerify",
		"noConnectionReuse",
	):
		if forbidden in k6_text:
			violations.append(f"k6 performance runner contains forbidden behavior: {forbidden}")

	runner_text = runner_path.read_text(encoding="utf-8")
	for token in (
		"normalize_base_url",
		"normalize_output_path",
		"CHILD_ENVIRONMENT_ALLOWLIST",
		"build_child_environment",
		"IONE_PERF_API_KEY",
		"IONE_PERF_API_SECRET",
		"k6 executable was not found",
		"k6 {registry.policy.k6_version} is required",
		"evaluate_performance_run",
		"subprocess.run",
		"--dry-run",
	):
		if token not in runner_text:
			violations.append(f"performance orchestration runner is missing: {token}")
	for forbidden in (
		"--api-key",
		"--api-secret",
		"verify=False",
		"ssl._create_unverified_context",
		"os.environ.copy()",
	):
		if forbidden in runner_text:
			violations.append(f"performance runner contains forbidden secret/TLS option: {forbidden}")

	performance_api = "/api/method/medops.api.v1.performance.get_performance_baseline_contract"
	for catalog_path in catalog_paths:
		if performance_api not in catalog_path.read_text(encoding="utf-8"):
			violations.append(
				f"{catalog_path.relative_to(root)} is missing the performance baseline endpoint"
			)
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	path_contract = openapi.get("paths", {}).get(performance_api, {})
	operation = path_contract.get("get", {})
	if operation.get("x-transaction-boundary") != "Read-only":
		violations.append("PLT-018 performance baseline contract API must be read-only")
	if operation.get("x-required-role") != "System Manager or HRP System Manager":
		violations.append("PLT-018 performance baseline contract API has the wrong role contract")
	if set(path_contract) != {"get"}:
		violations.append("PLT-018 performance baseline contract must not expose writes")
	return violations


def validate_software_supply_chain_contract(root: Path) -> list[str]:
	config_path = root / APP_NAME / "config" / "software_supply_chain.json"
	common_path = root / APP_NAME / "common" / "software_supply_chain.py"
	service_path = root / APP_NAME / "services" / "software_supply_chain.py"
	facade_path = root / APP_NAME / "hrp_foundation" / "services" / "security.py"
	api_path = root / APP_NAME / "api" / "v1" / "security.py"
	runner_path = root / "scripts" / "security_supply_chain.py"
	gitleaks_ignore_path = root / ".gitleaksignore"
	documentation_path = root / "architecture" / "software_supply_chain.md"
	adr_path = root / "architecture" / "adr" / "ADR-0011-governed-build-time-sbom-and-security-gates.md"
	task_path = root / "backlog" / "COD-016.md"
	change_path = root / "changes" / "COD-016.json"
	test_paths = (
		root / "tests" / "test_software_supply_chain.py",
		root / APP_NAME / "hrp_foundation" / "tests" / "test_software_supply_chain.py",
	)
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		config_path,
		common_path,
		service_path,
		facade_path,
		api_path,
		runner_path,
		gitleaks_ignore_path,
		documentation_path,
		adr_path,
		task_path,
		change_path,
		*test_paths,
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing software supply chain contract file: {path}" for path in missing]

	violations: list[str] = []
	try:
		policy = load_software_supply_chain_policy(config_path)
	except SoftwareSupplyChainContractError as exc:
		return [f"invalid software supply chain policy: {exc}"]
	if (
		policy.execution.http_write_enabled
		or policy.execution.site_execution_enabled
		or policy.execution.production_execution_enabled
	):
		violations.append("software supply chain scans must remain external to every Frappe site")
	if policy.sbom.contains_personal_data:
		violations.append("software bill of materials must not contain personal data")
	if policy.sbom.spec_version != "1.7":
		violations.append("software bill of materials must use CycloneDX 1.7")
	if set(policy.sbom.required_components) != {APP_NAME, "frappe", "erpnext", "hrms"}:
		violations.append("SBOM must include medops and all three locked upstream applications")
	for tool_name in ("gitleaks", "grype", "cyclonedx_cli"):
		tool = policy.tool(tool_name)
		if not tool.linux_asset or not tool.linux_sha256:
			violations.append(f"{tool_name} must pin an immutable Linux asset and SHA-256")
	if policy.gates.maximum_secret_findings != 0:
		violations.append("secret scan gate must fail on every unapproved finding")
	if policy.gates.maximum_denied_licenses != 0:
		violations.append("denied license gate must fail on every unapproved finding")

	common_text = common_path.read_text(encoding="utf-8")
	for token in (
		"class SoftwareSupplyChainPolicy",
		"class SecurityExecutionPolicy",
		"class SecurityGates",
		"class SecurityException",
		"compose_cyclonedx_sbom",
		"validate_cyclonedx_sbom",
		"evaluate_security_reports",
		"load_composition_inputs",
	):
		if token not in common_text:
			violations.append(f"software supply chain model is missing: {token}")

	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		"require_roles",
		"load_software_supply_chain_policy",
		"software_supply_chain_contract_read",
		'"scan_available_from_site"] = False',
		'"artifact_storage"] = "ci_or_release_artifact"',
	):
		if token not in service_text:
			violations.append(f"software supply chain service is missing: {token}")
	for forbidden in ("subprocess", "frappe.db.commit", "compose_cyclonedx_sbom"):
		if forbidden in service_text:
			violations.append(f"site security service contains forbidden execution behavior: {forbidden}")

	api_text = api_path.read_text(encoding="utf-8")
	if '@frappe.whitelist(allow_guest=True, methods=["GET"])' not in api_text:
		violations.append("PLT-019 software supply chain contract must be GET-only")
	for forbidden in ("run_security", "scan_repository", "compose_cyclonedx_sbom", "subprocess"):
		if forbidden in api_text:
			violations.append(
				f"software supply chain execution must not be exposed through HTTP: {forbidden}"
			)

	runner_text = runner_path.read_text(encoding="utf-8")
	for token in (
		"normalize_artifact_directory",
		"build_child_environment",
		"verify_tool_versions",
		'(npm_bin, "sbom", "--sbom-format", "cyclonedx")',
		"pip_audit",
		"bandit",
		"gitleaks",
		"grype",
		"cyclonedx",
		"evaluate_security_reports",
		"SHA256SUMS",
		"subprocess.run",
		"redacted_gitleaks_diagnostics",
	):
		if token not in runner_text:
			violations.append(f"software supply chain runner is missing: {token}")
	for forbidden in (
		"shell=True",
		"os.environ.copy()",
		"--api-key",
		"--api-secret",
		"--token",
		"verify=False",
		"ssl._create_unverified_context",
	):
		if forbidden in runner_text:
			violations.append(f"software supply chain runner contains forbidden behavior: {forbidden}")

	expected_fingerprint = (
		"2403cc6f43ebaaf0a924adaaac42cd5f3b217027:"
		# This fingerprint identifies an immutable historical commit before the app rename.
		"ione_hrp/hrp_foundation/tests/test_test_data_factory.py:generic-api-key:90"
	)
	gitleaks_entries = [
		line.strip()
		for line in gitleaks_ignore_path.read_text(encoding="utf-8").splitlines()
		if line.strip() and not line.lstrip().startswith("#")
	]
	if gitleaks_entries != [expected_fingerprint]:
		violations.append("Gitleaks ignore file must contain only the reviewed exact fingerprint")

	security_api = "/api/method/medops.api.v1.security.get_software_supply_chain_contract"
	for catalog_path in catalog_paths:
		if security_api not in catalog_path.read_text(encoding="utf-8"):
			violations.append(
				f"{catalog_path.relative_to(root)} is missing the software supply chain endpoint"
			)
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	path_contract = openapi.get("paths", {}).get(security_api, {})
	operation = path_contract.get("get", {})
	if operation.get("x-transaction-boundary") != "Read-only":
		violations.append("PLT-019 software supply chain contract API must be read-only")
	if operation.get("x-required-role") != "System Manager or HRP System Manager":
		violations.append("PLT-019 software supply chain contract API has the wrong role contract")
	if set(path_contract) != {"get"}:
		violations.append("PLT-019 software supply chain contract must not expose writes")
	return violations


def validate_system_settings_contract(root: Path) -> list[str]:
	doctype_directory = root / APP_NAME / "hrp_foundation" / "doctype" / "hrp_system_settings"
	doctype_json_path = doctype_directory / "hrp_system_settings.json"
	controller_path = doctype_directory / "hrp_system_settings.py"
	common_path = root / APP_NAME / "common" / "system_settings.py"
	service_path = root / APP_NAME / "hrp_foundation" / "services" / "system_settings.py"
	api_path = root / APP_NAME / "api" / "v1" / "settings.py"
	setup_path = root / APP_NAME / "setup" / "settings.py"
	install_path = root / APP_NAME / "setup" / "install.py"
	workspace_path = root / APP_NAME / "hrp_foundation" / "workspace" / "hrp" / "hrp.json"
	blueprint_path = root / "doctype_blueprints" / "hrp_foundation" / "hrp_system_settings.json"
	documentation_path = root / "architecture" / "system_settings.md"
	task_path = root / "backlog" / "COD-017.md"
	change_path = root / "changes" / "COD-017.json"
	test_paths = (
		root / "tests" / "test_system_settings.py",
		root / APP_NAME / "hrp_foundation" / "tests" / "test_system_settings.py",
	)
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		doctype_json_path,
		controller_path,
		common_path,
		service_path,
		api_path,
		setup_path,
		install_path,
		workspace_path,
		blueprint_path,
		documentation_path,
		task_path,
		change_path,
		*test_paths,
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing system settings contract file: {path}" for path in missing]

	violations: list[str] = []
	doctype = json.loads(doctype_json_path.read_text(encoding="utf-8"))
	if doctype.get("name") != "HRP System Settings" or doctype.get("issingle") != 1:
		violations.append("HRP System Settings must remain a standard Single DocType")
	if doctype.get("module") != "HRP Foundation":
		violations.append("HRP System Settings must remain owned by HRP Foundation")
	fields = {field["fieldname"]: field for field in doctype.get("fields", []) if field.get("fieldname")}
	expected_business_fields = {
		"enabled",
		"release_channel",
		"configuration_version",
		"default_company",
		"default_hospital",
		"strict_data_scope",
		"require_human_confirmation_for_ai",
		"integration_timeout_seconds",
		"remarks",
	}
	actual_business_fields = {
		fieldname
		for fieldname, field in fields.items()
		if field.get("fieldtype") not in {"Section Break", "Column Break", "Tab Break"}
	}
	if actual_business_fields != expected_business_fields:
		violations.append("HRP System Settings has an unexpected mutable schema")
	if "configuration_json" in fields:
		violations.append("HRP System Settings must not expose arbitrary JSON configuration")
	for fieldname, default in (
		("release_channel", LOCKED_RELEASE_CHANNEL),
		("strict_data_scope", "1"),
		("require_human_confirmation_for_ai", "1"),
		("configuration_version", "1"),
	):
		field = fields.get(fieldname, {})
		if field.get("read_only") != 1 or str(field.get("default")) != default:
			violations.append(f"{fieldname} must be a locked read-only system setting")
	timeout = fields.get("integration_timeout_seconds", {})
	if timeout.get("fieldtype") != "Int" or str(timeout.get("default")) != "30":
		violations.append("integration timeout must be an explicit integer defaulting to 30")
	if fields.get("default_company", {}).get("options") != "Company":
		violations.append("default company must use the standard Company Link")
	default_hospital = fields.get("default_hospital", {})
	if default_hospital.get("fieldtype") != "Link" or default_hospital.get("options") != "HRP Hospital":
		violations.append("default hospital must link to COD-018 HRP Hospital")

	permissions = doctype.get("permissions", [])
	if {permission.get("role") for permission in permissions} != {
		"System Manager",
		"HRP System Manager",
	}:
		violations.append("system settings permissions must be limited to both system admin roles")
	for permission in permissions:
		if not all(permission.get(action) == 1 for action in ("read", "write", "create")):
			violations.append("system settings admin roles must have read, write and create")
		if permission.get("delete"):
			violations.append("system settings must not grant delete permission")

	common_text = common_path.read_text(encoding="utf-8")
	for token in (
		"class SystemSettingsUpdate",
		"class SystemSettingsState",
		"build_system_settings_update",
		"build_system_settings_state",
		"changed_mutable_fields",
		f"MIN_INTEGRATION_TIMEOUT_SECONDS = {MIN_INTEGRATION_TIMEOUT_SECONDS}",
		f"MAX_INTEGRATION_TIMEOUT_SECONDS = {MAX_INTEGRATION_TIMEOUT_SECONDS}",
	):
		if token not in common_text:
			violations.append(f"system settings contract model is missing: {token}")

	controller_text = controller_path.read_text(encoding="utf-8")
	for token in (
		"def lock_configuration",
		"FOR UPDATE",
		"configuration_version",
		"OPERATION_NOT_ALLOWED",
		"as_contract_state",
	):
		if token not in controller_text:
			violations.append(f"system settings controller is missing: {token}")
	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		"class UpdateSystemSettingsService",
		"DomainService",
		"SYSTEM_SETTINGS_ADMIN_ROLES",
		"changed_mutable_fields",
		"system_settings_changed",
		"changed_fields",
	):
		if token not in service_text:
			violations.append(f"system settings service is missing: {token}")
	for forbidden in ("frappe.db.commit", "configuration_json"):
		if forbidden in service_text:
			violations.append(f"system settings service contains forbidden behavior: {forbidden}")

	api_text = api_path.read_text(encoding="utf-8")
	if api_text.count('@frappe.whitelist(methods=["GET"])') != 1:
		violations.append("PLT-020 system settings API must expose one GET method")
	if api_text.count('@frappe.whitelist(methods=["POST"])') != 1:
		violations.append("PLT-021 system settings API must expose one POST method")
	if "allow_guest=True" in api_text:
		violations.append("system settings APIs must never allow guests")
	for token in (
		"build_system_settings_update",
		"idempotency_key=None",
		"expected_version",
	):
		if token not in api_text:
			violations.append(f"system settings API is missing: {token}")

	setup_text = setup_path.read_text(encoding="utf-8")
	install_text = install_path.read_text(encoding="utf-8")
	if "def ensure_system_settings" not in setup_text:
		violations.append("system settings migration repair is missing")
	if install_text.count("ensure_system_settings()") != 2:
		violations.append("system settings repair must run after install and migrate")

	blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))
	blueprint_fields = {field["fieldname"] for field in blueprint.get("fields", []) if field.get("fieldname")}
	if blueprint_fields != expected_business_fields:
		violations.append("system settings blueprint must match the runtime business fields")
	workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
	if not {
		"System Manager",
		"HRP System Manager",
	}.issubset({role.get("role") for role in workspace.get("roles", [])}):
		violations.append("HRP workspace must expose settings to both system admin roles")
	if not any(
		link.get("link_to") == "HRP System Settings" and link.get("label") == "系统设置"
		for link in workspace.get("links", [])
	):
		violations.append("HRP workspace must include the Chinese system settings link")

	get_api = "/api/method/medops.api.v1.settings.get_system_settings"
	post_api = "/api/method/medops.api.v1.settings.update_system_settings"
	for catalog_path in catalog_paths:
		catalog_text = catalog_path.read_text(encoding="utf-8")
		for endpoint in (get_api, post_api):
			if endpoint not in catalog_text:
				violations.append(
					f"{catalog_path.relative_to(root)} is missing system settings endpoint {endpoint}"
				)
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	get_contract = openapi.get("paths", {}).get(get_api, {})
	get_operation = get_contract.get("get", {})
	if set(get_contract) != {"get"}:
		violations.append("PLT-020 system settings contract must be GET-only")
	if get_operation.get("x-transaction-boundary") != "Read-only":
		violations.append("PLT-020 system settings API must be read-only")
	if get_operation.get("x-required-role") != "System Manager or HRP System Manager":
		violations.append("PLT-020 system settings API has the wrong role contract")
	post_contract = openapi.get("paths", {}).get(post_api, {})
	post_operation = post_contract.get("post", {})
	if set(post_contract) != {"post"}:
		violations.append("PLT-021 system settings contract must be POST-only")
	if post_operation.get("x-transaction-boundary") != "Single DB transaction":
		violations.append("PLT-021 system settings API needs one transaction")
	if post_operation.get("x-idempotency") != "Required for write":
		violations.append("PLT-021 system settings API must require idempotency")
	if post_operation.get("x-required-role") != "System Manager or HRP System Manager":
		violations.append("PLT-021 system settings API has the wrong role contract")
	request_schema = (
		post_operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
	)
	if request_schema.get("additionalProperties") is not False:
		violations.append("PLT-021 system settings request must reject unknown fields")
	if set(request_schema.get("required", [])) != {
		"enabled",
		"integration_timeout_seconds",
		"expected_version",
	}:
		violations.append("PLT-021 system settings request has the wrong required fields")
	return violations


def validate_organization_hierarchy_contract(root: Path) -> list[str]:
	module_root = root / APP_NAME / "hrp_organization"
	common_path = root / APP_NAME / "common" / "organization.py"
	service_path = module_root / "services" / "organization.py"
	api_path = root / APP_NAME / "api" / "v1" / "organization.py"
	setup_path = root / APP_NAME / "setup" / "organization.py"
	install_path = root / APP_NAME / "setup" / "install.py"
	workspace_path = module_root / "workspace" / "hrp_organization" / "hrp_organization.json"
	documentation_path = root / "architecture" / "organization_hierarchy.md"
	adr_path = root / "architecture" / "adr" / "ADR-0012-versioned-hospital-organization-hierarchy.md"
	task_path = root / "backlog" / "COD-018.md"
	change_path = root / "changes" / "COD-018.json"
	pure_test_path = root / "tests" / "test_organization.py"
	integration_test_path = module_root / "tests" / "test_organization.py"
	doctype_names = (
		("hrp_hospital", "HRP Hospital"),
		("hrp_organization_version", "HRP Organization Version"),
		("hrp_organization_unit", "HRP Organization Unit"),
	)
	doctype_paths = {
		name: (
			module_root / "doctype" / directory / f"{directory}.json",
			module_root / "doctype" / directory / f"{directory}.py",
			root / "doctype_blueprints" / "hrp_organization" / f"{directory}.json",
		)
		for directory, name in doctype_names
	}
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		common_path,
		service_path,
		api_path,
		setup_path,
		install_path,
		workspace_path,
		documentation_path,
		adr_path,
		task_path,
		change_path,
		pure_test_path,
		integration_test_path,
		*catalog_paths,
		*(path for paths in doctype_paths.values() for path in paths),
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing organization hierarchy contract file: {path}" for path in missing]

	violations: list[str] = []
	integration_test_text = integration_test_path.read_text(encoding="utf-8")
	if "erpnext.setup.doctype.company.test_company" in integration_test_text:
		violations.append("organization tests must not import side-effectful upstream test fixtures")
	expected_roles = {"System Manager", "HRP System Manager", "HRP Data Steward"}
	for name, (doctype_path, controller_path, blueprint_path) in doctype_paths.items():
		doctype = json.loads(doctype_path.read_text(encoding="utf-8"))
		blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))
		if "?" in blueprint_path.read_text(encoding="utf-8"):
			violations.append(f"{name} blueprint must not contain replacement question marks")
		if doctype.get("name") != name or doctype.get("module") != "HRP Organization":
			violations.append(f"{name} must be owned by HRP Organization")
		if blueprint.get("name") != name or blueprint.get("module") != "HRP Organization":
			violations.append(f"{name} blueprint must match its runtime owner")
		runtime_fields = {
			field.get("fieldname")
			for field in doctype.get("fields", [])
			if field.get("fieldname") and field.get("fieldtype") not in {"Section Break", "Column Break"}
		}
		blueprint_fields = {
			field.get("fieldname") for field in blueprint.get("fields", []) if field.get("fieldname")
		}
		if runtime_fields != blueprint_fields:
			violations.append(f"{name} blueprint fields must match runtime metadata")
		permissions = doctype.get("permissions", [])
		if {permission.get("role") for permission in permissions} != expected_roles:
			violations.append(f"{name} must use the three organization governance roles")
		if not all(permission.get("read") == 1 for permission in permissions):
			violations.append(f"{name} must remain readable by every organization governance role")
		controller_text = controller_path.read_text(encoding="utf-8")
		if "frappe.db.commit" in controller_text:
			violations.append(f"{name} controller must not commit transactions")

	hospital = json.loads(doctype_paths["HRP Hospital"][0].read_text(encoding="utf-8"))
	hospital_fields = {field["fieldname"]: field for field in hospital.get("fields", [])}
	if hospital.get("autoname") != "field:code":
		violations.append("HRP Hospital must keep its stable canonical code as name")
	if hospital_fields.get("company", {}).get("options") != "Company":
		violations.append("HRP Hospital company must use the standard Company Link")
	for permission in hospital.get("permissions", []):
		if not all(permission.get(action) == 1 for action in ("read", "write", "create")):
			violations.append("HRP Hospital governance roles must read, create and write")
		if permission.get("delete"):
			violations.append("HRP Hospital must not grant delete permission")

	version = json.loads(doctype_paths["HRP Organization Version"][0].read_text(encoding="utf-8"))
	version_fields = {field["fieldname"]: field for field in version.get("fields", [])}
	if version.get("is_submittable") != 1:
		violations.append("HRP Organization Version must remain submittable for immutable publication")
	if version_fields.get("hospital", {}).get("options") != "HRP Hospital":
		violations.append("HRP Organization Version must link to HRP Hospital")
	for permission in version.get("permissions", []):
		if any(permission.get(action) for action in ("write", "create", "delete", "submit", "cancel")):
			violations.append("HRP Organization Version writes must remain service-only")

	unit = json.loads(doctype_paths["HRP Organization Unit"][0].read_text(encoding="utf-8"))
	unit_fields = {field["fieldname"]: field for field in unit.get("fields", [])}
	if unit.get("is_tree") != 1 or unit.get("nsm_parent_field") != "parent_organization_unit":
		violations.append("HRP Organization Unit must remain a NestedSet tree")
	if unit_fields.get("organization_version", {}).get("options") != "HRP Organization Version":
		violations.append("HRP Organization Unit must be scoped by organization version")
	if tuple(unit_fields.get("unit_type", {}).get("options", "").splitlines()) != UNIT_TYPES:
		violations.append("HRP Organization Unit types must match the public organization contract")
	for permission in unit.get("permissions", []):
		if any(permission.get(action) for action in ("write", "create", "delete", "submit", "cancel")):
			violations.append("HRP Organization Unit writes must remain service-only")

	common_text = common_path.read_text(encoding="utf-8")
	for token in (
		f"ORGANIZATION_SCHEMA_VERSION = {ORGANIZATION_SCHEMA_VERSION}",
		f"MAX_HIERARCHY_NODES = {MAX_HIERARCHY_NODES}",
		"class HospitalUpsert",
		"class OrganizationVersionCreate",
		"class HierarchyReplace",
		"class OrganizationVersionPublish",
		"normalize_hierarchy_nodes",
		"hierarchy_digest",
	):
		if token not in common_text:
			violations.append(f"organization public contract is missing: {token}")

	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		"class UpsertHospitalService",
		"class CreateOrganizationVersionService",
		"class ReplaceOrganizationHierarchyService",
		"class PublishOrganizationVersionService",
		"DomainService",
		"FOR UPDATE",
		"expected_revision",
		"hierarchy_digest",
		"require_roles",
	):
		if token not in service_text:
			violations.append(f"organization service is missing: {token}")
	if "frappe.db.commit" in service_text:
		violations.append("organization services must not commit transactions")

	api_text = api_path.read_text(encoding="utf-8")
	if api_text.count('@frappe.whitelist(methods=["POST"])') != 4:
		violations.append("COD-018 organization API must expose exactly four POST methods")
	if api_text.count('@frappe.whitelist(methods=["GET"])') != 1:
		violations.append("COD-018 organization API must expose exactly one GET method")
	if "allow_guest=True" in api_text:
		violations.append("organization APIs must never allow guests")

	setup_text = setup_path.read_text(encoding="utf-8")
	for token in (
		"def ensure_organization_hierarchy",
		"frappe.db.add_unique",
		"_legacy_hospital_code",
		"default_hospital_migrated",
	):
		if token not in setup_text:
			violations.append(f"organization migration is missing: {token}")
	install_text = install_path.read_text(encoding="utf-8")
	if install_text.count("ensure_organization_hierarchy()") != 2:
		violations.append("organization migration must run after install and migrate")

	workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
	if {role.get("role") for role in workspace.get("roles", [])} != expected_roles:
		violations.append("organization workspace must use the three governance roles")
	expected_links = {"HRP Hospital", "HRP Organization Version", "HRP Organization Unit"}
	actual_links = {link.get("link_to") for link in workspace.get("links", []) if link.get("link_to")}
	if not expected_links.issubset(actual_links):
		violations.append("organization workspace must expose hospital, version and hierarchy")

	for catalog_path in (
		root / "design" / "doctype_catalog.csv",
		root / "design" / "field_catalog.csv",
	):
		with catalog_path.open(encoding="utf-8-sig", newline="") as stream:
			for row in csv.DictReader(stream):
				if row.get("doctype") in expected_links and any("?" in str(value) for value in row.values()):
					violations.append(
						f"{catalog_path.relative_to(root)} contains replacement question marks "
						f"for {row.get('doctype')}"
					)
	settings_blueprint = root / "doctype_blueprints" / "hrp_foundation" / "hrp_system_settings.json"
	if '"label": "默认医院"' not in settings_blueprint.read_text(encoding="utf-8"):
		violations.append("system settings blueprint must retain the Chinese default hospital label")

	endpoints = {
		"/api/method/medops.api.v1.organization.upsert_hospital": "post",
		"/api/method/medops.api.v1.organization.create_organization_version": "post",
		"/api/method/medops.api.v1.organization.replace_organization_hierarchy": "post",
		"/api/method/medops.api.v1.organization.publish_organization_version": "post",
		"/api/method/medops.api.v1.organization.get_organization_hierarchy": "get",
	}
	for catalog_path in catalog_paths:
		catalog_text = catalog_path.read_text(encoding="utf-8")
		if "??" in catalog_text:
			violations.append(f"{catalog_path.relative_to(root)} contains replacement question marks")
		for endpoint in endpoints:
			if endpoint not in catalog_text:
				violations.append(
					f"{catalog_path.relative_to(root)} is missing organization endpoint {endpoint}"
				)
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	expected_role_contract = "System Manager or HRP System Manager or HRP Data Steward"
	for endpoint, method in endpoints.items():
		path_contract = openapi.get("paths", {}).get(endpoint, {})
		operation = path_contract.get(method, {})
		if set(path_contract) != {method}:
			violations.append(f"{endpoint} must expose only {method.upper()}")
		if operation.get("x-required-role") != expected_role_contract:
			violations.append(f"{endpoint} has the wrong organization role contract")
		if method == "post":
			if operation.get("x-transaction-boundary") != "Single DB transaction":
				violations.append(f"{endpoint} must use one database transaction")
			if operation.get("x-idempotency") != "Required for write":
				violations.append(f"{endpoint} must require idempotency")
			headers = operation.get("parameters", [])
			if not any(
				header.get("name") == "Idempotency-Key" and header.get("required") is True
				for header in headers
			):
				violations.append(f"{endpoint} must declare a required Idempotency-Key header")
		elif operation.get("x-transaction-boundary") != "Read-only":
			violations.append(f"{endpoint} must remain read-only")
	return violations


def validate_organization_mapping_contract(root: Path) -> list[str]:
	module_root = root / APP_NAME / "hrp_organization"
	common_path = root / APP_NAME / "common" / "organization_mapping.py"
	service_path = module_root / "services" / "organization_mapping.py"
	api_path = root / APP_NAME / "api" / "v1" / "organization_mapping.py"
	setup_path = root / APP_NAME / "setup" / "organization.py"
	controller_path = module_root / "doctype" / "hrp_organization_mapping" / "hrp_organization_mapping.py"
	doctype_path = controller_path.with_suffix(".json")
	blueprint_path = root / "doctype_blueprints" / "hrp_organization" / "hrp_organization_mapping.json"
	workspace_path = module_root / "workspace" / "hrp_organization" / "hrp_organization.json"
	documentation_path = root / "architecture" / "organization_mapping.md"
	adr_path = root / "architecture" / "adr" / "ADR-0013-version-scoped-standard-organization-mapping.md"
	task_path = root / "backlog" / "COD-019.md"
	change_path = root / "changes" / "COD-019.json"
	pure_test_path = root / "tests" / "test_organization_mapping.py"
	integration_test_path = module_root / "tests" / "test_organization_mapping.py"
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		common_path,
		service_path,
		api_path,
		setup_path,
		controller_path,
		doctype_path,
		blueprint_path,
		workspace_path,
		documentation_path,
		adr_path,
		task_path,
		change_path,
		pure_test_path,
		integration_test_path,
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing organization mapping contract file: {path}" for path in missing]

	violations: list[str] = []
	doctype = json.loads(doctype_path.read_text(encoding="utf-8"))
	blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))
	if doctype.get("name") != ORGANIZATION_MAPPING_DOCTYPE:
		violations.append("organization mapping runtime metadata has the wrong DocType name")
	if doctype.get("module") != "HRP Organization":
		violations.append("organization mapping must be owned by HRP Organization")
	if blueprint.get("name") != ORGANIZATION_MAPPING_DOCTYPE:
		violations.append("organization mapping blueprint has the wrong DocType name")
	if blueprint.get("module") != "HRP Organization":
		violations.append("organization mapping blueprint must be owned by HRP Organization")
	if "?" in blueprint_path.read_text(encoding="utf-8"):
		violations.append("organization mapping blueprint must not contain replacement question marks")

	runtime_fields = {
		field.get("fieldname")
		for field in doctype.get("fields", [])
		if field.get("fieldname") and field.get("fieldtype") not in {"Section Break", "Column Break"}
	}
	blueprint_fields = {
		field.get("fieldname") for field in blueprint.get("fields", []) if field.get("fieldname")
	}
	expected_fields = {
		"organization_version",
		"organization_unit",
		"company",
		"hospital",
		"unit_code",
		"unit_type",
		"department",
		"cost_center",
		"enabled",
		"revision",
		"remarks",
	}
	if runtime_fields != expected_fields or blueprint_fields != expected_fields:
		violations.append("organization mapping runtime and blueprint fields must match the contract")
	field_map = {field["fieldname"]: field for field in doctype.get("fields", []) if field.get("fieldname")}
	for fieldname, target in (
		("organization_version", "HRP Organization Version"),
		("organization_unit", "HRP Organization Unit"),
		("company", "Company"),
		("hospital", "HRP Hospital"),
		("department", "Department"),
		("cost_center", "Cost Center"),
	):
		if field_map.get(fieldname, {}).get("options") != target:
			violations.append(f"organization mapping {fieldname} must link to {target}")
	if doctype.get("autoname") != "field:organization_unit":
		violations.append("organization mapping must use the organization unit as stable name")

	expected_roles = {
		"System Manager",
		"HRP System Manager",
		"HRP Data Steward",
		"HRP Integration User",
	}
	permissions = doctype.get("permissions", [])
	if {permission.get("role") for permission in permissions} != expected_roles:
		violations.append("organization mapping must use its four read roles")
	for permission in permissions:
		if permission.get("read") != 1:
			violations.append("organization mapping roles must retain read access")
		if any(
			permission.get(action) for action in ("write", "create", "delete", "submit", "cancel", "amend")
		):
			violations.append("organization mapping writes must remain service-only")

	common_text = common_path.read_text(encoding="utf-8")
	for token in (
		f'ORGANIZATION_MAPPING_DOCTYPE = "{ORGANIZATION_MAPPING_DOCTYPE}"',
		f"ORGANIZATION_MAPPING_SCHEMA_VERSION = {ORGANIZATION_MAPPING_SCHEMA_VERSION}",
		"class OrganizationMappingUpsert",
		"class OrganizationMappingResolve",
		"remarks_digest",
	):
		if token not in common_text:
			violations.append(f"organization mapping public contract is missing: {token}")

	controller_text = controller_path.read_text(encoding="utf-8")
	for token in (
		"organization_mapping_service_write",
		"expected_revision",
		"Published",
		"as_public_dict",
	):
		if token not in controller_text:
			violations.append(f"organization mapping controller is missing: {token}")
	if "frappe.db.commit" in controller_text:
		violations.append("organization mapping controller must not commit transactions")

	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		"class UpsertOrganizationMappingService",
		"class ResolveOrganizationMappingService",
		"DomainService",
		"FOR UPDATE",
		"expected_revision",
		"STANDARD_TARGETS",
		"_validate_target_uniqueness",
		"_validate_tree_alignment",
		"require_enabled",
	):
		if token not in service_text:
			violations.append(f"organization mapping service is missing: {token}")
	if "frappe.db.commit" in service_text:
		violations.append("organization mapping services must not commit transactions")
	if "schema_version=ORGANIZATION_MAPPING_SCHEMA_VERSION" in service_text:
		violations.append("organization mapping audit must not reuse reserved schema_version")

	api_text = api_path.read_text(encoding="utf-8")
	if api_text.count('@frappe.whitelist(methods=["POST"])') != 1:
		violations.append("COD-019 mapping API must expose exactly one POST method")
	if api_text.count('@frappe.whitelist(methods=["GET"])') != 1:
		violations.append("COD-019 mapping API must expose exactly one GET method")
	if "allow_guest=True" in api_text:
		violations.append("organization mapping APIs must never allow guests")

	setup_text = setup_path.read_text(encoding="utf-8")
	for token in (
		"uniq_hrp_org_mapping_version_unit",
		"uniq_hrp_org_mapping_version_department",
		"uniq_hrp_org_mapping_version_cost_center",
	):
		if token not in setup_text:
			violations.append(f"organization mapping migration is missing: {token}")

	workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
	actual_links = {link.get("link_to") for link in workspace.get("links", []) if link.get("link_to")}
	if ORGANIZATION_MAPPING_DOCTYPE not in actual_links:
		violations.append("organization workspace must expose standard mappings")

	with (root / "design" / "doctype_catalog.csv").open(
		encoding="utf-8-sig",
		newline="",
	) as stream:
		doctype_rows = [
			row for row in csv.DictReader(stream) if row.get("doctype") == ORGANIZATION_MAPPING_DOCTYPE
		]
	if len(doctype_rows) != 1:
		violations.append("organization mapping must have exactly one DocType catalog row")
	with (root / "design" / "field_catalog.csv").open(
		encoding="utf-8-sig",
		newline="",
	) as stream:
		field_rows = [
			row for row in csv.DictReader(stream) if row.get("doctype") == ORGANIZATION_MAPPING_DOCTYPE
		]
	if {row.get("fieldname") for row in field_rows} != expected_fields:
		violations.append("organization mapping field catalog must match runtime metadata")

	endpoints = {
		"/api/method/medops.api.v1.organization_mapping.upsert_organization_mapping": "post",
		"/api/method/medops.api.v1.organization_mapping.resolve_organization_mapping": "get",
	}
	for catalog_path in catalog_paths:
		catalog_text = catalog_path.read_text(encoding="utf-8")
		for endpoint in endpoints:
			if endpoint not in catalog_text:
				violations.append(
					f"{catalog_path.relative_to(root)} is missing organization mapping endpoint {endpoint}"
				)
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	write_role = "System Manager or HRP System Manager or HRP Data Steward"
	read_role = f"{write_role} or HRP Integration User"
	for endpoint, method in endpoints.items():
		path_contract = openapi.get("paths", {}).get(endpoint, {})
		operation = path_contract.get(method, {})
		if set(path_contract) != {method}:
			violations.append(f"{endpoint} must expose only {method.upper()}")
		expected_role = write_role if method == "post" else read_role
		if operation.get("x-required-role") != expected_role:
			violations.append(f"{endpoint} has the wrong organization mapping role contract")
		if method == "post":
			if operation.get("x-transaction-boundary") != "Single DB transaction":
				violations.append(f"{endpoint} must use one database transaction")
			if operation.get("x-idempotency") != "Required for write":
				violations.append(f"{endpoint} must require idempotency")
			if not any(
				header.get("name") == "Idempotency-Key" and header.get("required") is True
				for header in operation.get("parameters", [])
			):
				violations.append(f"{endpoint} must declare a required Idempotency-Key header")
		else:
			if operation.get("x-transaction-boundary") != "Read-only":
				violations.append(f"{endpoint} must remain read-only")
			if operation.get("x-idempotency") != "Read-only deterministic":
				violations.append(f"{endpoint} must remain deterministic")
	return violations


def validate_master_data_governance_contract(root: Path) -> list[str]:
	module_root = root / APP_NAME / "hrp_master_data"
	common_path = root / APP_NAME / "common" / "master_data.py"
	service_path = module_root / "services" / "master_data.py"
	permission_path = module_root / "permissions.py"
	api_path = root / APP_NAME / "api" / "v1" / "master_data.py"
	setup_path = root / APP_NAME / "setup" / "master_data.py"
	workspace_path = module_root / "workspace" / "hrp_master_data" / "hrp_master_data.json"
	documentation_path = root / "architecture" / "master_data_change_requests.md"
	adr_path = root / "architecture" / "adr" / "ADR-0014-governed-master-data-change-proposals.md"
	task_path = root / "backlog" / "COD-020.md"
	change_path = root / "changes" / "COD-020.json"
	pure_test_path = root / "tests" / "test_master_data.py"
	integration_test_path = module_root / "tests" / "test_master_data.py"
	doctypes = {
		MASTER_DATA_DOMAIN_DOCTYPE: "hrp_master_data_domain",
		MASTER_DATA_REQUEST_DOCTYPE: "hrp_master_data_request",
		MASTER_DATA_CHANGE_ITEM_DOCTYPE: "hrp_master_data_change_item",
	}
	runtime_paths = {
		name: module_root / "doctype" / directory / f"{directory}.json"
		for name, directory in doctypes.items()
	}
	controller_paths = {
		name: module_root / "doctype" / directory / f"{directory}.py" for name, directory in doctypes.items()
	}
	blueprint_paths = {
		name: root / "doctype_blueprints" / "hrp_master_data" / f"{directory}.json"
		for name, directory in doctypes.items()
	}
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		common_path,
		service_path,
		permission_path,
		api_path,
		setup_path,
		workspace_path,
		documentation_path,
		adr_path,
		task_path,
		change_path,
		pure_test_path,
		integration_test_path,
		*runtime_paths.values(),
		*controller_paths.values(),
		*blueprint_paths.values(),
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing master data governance contract file: {path}" for path in missing]

	violations: list[str] = []
	expected_fields = {
		MASTER_DATA_DOMAIN_DOCTYPE: {
			"code",
			"display_name",
			"target_doctype",
			"enabled",
			"allow_create",
			"allow_update",
			"allow_disable",
			"allowed_fields",
			"policy_version",
			"policy_digest",
			"revision",
			"remarks",
		},
		MASTER_DATA_REQUEST_DOCTYPE: {
			"master_data_domain",
			"target_doctype",
			"operation",
			"target_name",
			"subject",
			"company",
			"hospital",
			"organization_unit",
			"effective_on",
			"changes",
			"proposal_digest",
			"baseline_modified_at",
			"request_status",
			"requested_by",
			"requested_at",
			"submitted_at",
			"reviewed_by",
			"reviewed_at",
			"decision_reason",
			"revision",
		},
		MASTER_DATA_CHANGE_ITEM_DOCTYPE: {
			"sequence_no",
			"field_name",
			"field_label",
			"value_type",
			"current_value",
			"proposed_value",
			"reason",
		},
	}
	for name in doctypes:
		runtime = json.loads(runtime_paths[name].read_text(encoding="utf-8"))
		blueprint_text = blueprint_paths[name].read_text(encoding="utf-8")
		blueprint = json.loads(blueprint_text)
		if "?" in blueprint_text:
			violations.append(f"{name} blueprint must not contain replacement question marks")
		if runtime.get("name") != name or blueprint.get("name") != name:
			violations.append(f"{name} runtime and blueprint names must match")
		if runtime.get("module") != "HRP Master Data" or blueprint.get("module") != "HRP Master Data":
			violations.append(f"{name} must be owned by HRP Master Data")
		runtime_fields = {
			field.get("fieldname")
			for field in runtime.get("fields", [])
			if field.get("fieldname") and field.get("fieldtype") not in {"Section Break", "Column Break"}
		}
		blueprint_fields = {
			field.get("fieldname") for field in blueprint.get("fields", []) if field.get("fieldname")
		}
		if runtime_fields != expected_fields[name] or blueprint_fields != expected_fields[name]:
			violations.append(f"{name} runtime and blueprint fields must match the contract")

	domain = json.loads(runtime_paths[MASTER_DATA_DOMAIN_DOCTYPE].read_text(encoding="utf-8"))
	request = json.loads(runtime_paths[MASTER_DATA_REQUEST_DOCTYPE].read_text(encoding="utf-8"))
	change_item = json.loads(runtime_paths[MASTER_DATA_CHANGE_ITEM_DOCTYPE].read_text(encoding="utf-8"))
	if domain.get("autoname") != "field:code" or domain.get("allow_rename"):
		violations.append("master data domain must have a stable non-renamable code")
	if request.get("is_submittable") != 1 or change_item.get("istable") != 1:
		violations.append("master data request must be submittable with a child change table")
	for metadata in (domain, request):
		for permission in metadata.get("permissions", []):
			if permission.get("read") != 1:
				violations.append(f"{metadata.get('name')} roles must retain read access")
			if any(
				permission.get(action)
				for action in ("write", "create", "delete", "submit", "cancel", "amend")
			):
				violations.append(f"{metadata.get('name')} writes must remain service-only")

	common_text = common_path.read_text(encoding="utf-8")
	for token in (
		f"MASTER_DATA_SCHEMA_VERSION = {MASTER_DATA_SCHEMA_VERSION}",
		"MASTER_DATA_TARGET_POLICIES",
		"SENSITIVE_FIELD_TOKENS",
		"MAX_CHANGE_ITEMS = 64",
		"MAX_CHANGE_PAYLOAD_BYTES = 64 * 1024",
		"class MasterDataDomainUpsert",
		"class MasterDataRequestUpsert",
		"class MasterDataRequestSubmit",
		"class MasterDataRequestReview",
	):
		if token not in common_text:
			violations.append(f"master data public contract is missing: {token}")
	for target in ("Department", "Cost Center", "Item", "Supplier", "Warehouse"):
		if target not in MASTER_DATA_TARGET_POLICIES or f'target_doctype="{target}"' not in common_text:
			violations.append(f"master data static policy is missing: {target}")

	for name, controller_path in controller_paths.items():
		controller_text = controller_path.read_text(encoding="utf-8")
		if name != MASTER_DATA_CHANGE_ITEM_DOCTYPE:
			for token in ("master_data_service_write", "lock_revision", "as_public_dict"):
				if token not in controller_text:
					violations.append(f"{name} controller is missing: {token}")
		if "frappe.db.commit" in controller_text:
			violations.append(f"{name} controller must not commit transactions")

	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		"class UpsertMasterDataDomainService",
		"class SaveMasterDataRequestService",
		"class SubmitMasterDataRequestService",
		"class ReviewMasterDataRequestService",
		"DomainService",
		"MASTER_DATA_TARGET_POLICIES",
		"FOR UPDATE",
		"expected_revision",
		"requested_by == frappe.session.user",
		"_assert_proposal_current",
		"_assert_organization_scope",
	):
		if token not in service_text:
			violations.append(f"master data service is missing: {token}")
	if "frappe.db.commit" in service_text:
		violations.append("master data services must not commit transactions")
	for forbidden in (
		'frappe.get_doc("Department"',
		'frappe.get_doc("Cost Center"',
		'frappe.get_doc("Item"',
		'frappe.get_doc("Supplier"',
		'frappe.get_doc("Warehouse"',
		"frappe.db.set_value",
	):
		if forbidden in service_text:
			violations.append(f"master data approval must not mutate standard records: {forbidden}")

	permission_text = permission_path.read_text(encoding="utf-8")
	hooks_text = (root / APP_NAME / "hooks.py").read_text(encoding="utf-8")
	for token in (
		"def master_data_request_query",
		"def can_read_master_data_request",
		"requested_by",
		"HRP User",
	):
		if token not in permission_text:
			violations.append(f"master data Desk permission contract is missing: {token}")
	for token in (
		'"HRP Master Data Request": "medops.hrp_master_data.permissions.master_data_request_query"',
		'"HRP Master Data Request": "medops.hrp_master_data.permissions.can_read_master_data_request"',
	):
		if token not in hooks_text:
			violations.append(f"master data permission hook is missing: {token}")

	api_text = api_path.read_text(encoding="utf-8")
	for method_name in (
		"upsert_master_data_domain",
		"save_master_data_request",
		"submit_master_data_request",
		"review_master_data_request",
		"get_master_data_request",
	):
		if f"def {method_name}(" not in api_text:
			violations.append(f"COD-020 API is missing method: {method_name}")
	if api_text.count('@frappe.whitelist(methods=["POST"])') < 4:
		violations.append("COD-020 API must retain at least four POST methods")
	if api_text.count('@frappe.whitelist(methods=["GET"])') < 1:
		violations.append("COD-020 API must retain at least one GET method")
	if "allow_guest=True" in api_text:
		violations.append("master data APIs must never allow guests")

	setup_text = setup_path.read_text(encoding="utf-8")
	for token in (
		"uniq_hrp_master_data_domain_target",
		"idx_hrp_master_data_request_target_status",
	):
		if token not in setup_text:
			violations.append(f"master data migration is missing: {token}")

	workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
	actual_links = {link.get("link_to") for link in workspace.get("links", []) if link.get("link_to")}
	if not {MASTER_DATA_DOMAIN_DOCTYPE, MASTER_DATA_REQUEST_DOCTYPE}.issubset(actual_links):
		violations.append("master data workspace must expose domains and requests")

	with (root / "design" / "doctype_catalog.csv").open(
		encoding="utf-8-sig",
		newline="",
	) as stream:
		doctype_rows = [row for row in csv.DictReader(stream) if row.get("doctype") in expected_fields]
	if {row.get("doctype") for row in doctype_rows} != set(expected_fields):
		violations.append("master data DocType catalog must include all three governed objects")
	with (root / "design" / "field_catalog.csv").open(
		encoding="utf-8-sig",
		newline="",
	) as stream:
		field_rows = [row for row in csv.DictReader(stream) if row.get("doctype") in expected_fields]
	for name, fields in expected_fields.items():
		if {row.get("fieldname") for row in field_rows if row.get("doctype") == name} != fields:
			violations.append(f"{name} field catalog must match runtime metadata")

	endpoints = {
		"/api/method/medops.api.v1.master_data.upsert_master_data_domain": "post",
		"/api/method/medops.api.v1.master_data.save_master_data_request": "post",
		"/api/method/medops.api.v1.master_data.submit_master_data_request": "post",
		"/api/method/medops.api.v1.master_data.review_master_data_request": "post",
		"/api/method/medops.api.v1.master_data.get_master_data_request": "get",
	}
	for catalog_path in catalog_paths:
		catalog_text = catalog_path.read_text(encoding="utf-8")
		for endpoint in endpoints:
			if endpoint not in catalog_text:
				violations.append(
					f"{catalog_path.relative_to(root)} is missing master data endpoint {endpoint}"
				)
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	admin_role = "System Manager or HRP System Manager or HRP Data Steward"
	requester_role = f"{admin_role} or HRP User"
	for endpoint, method in endpoints.items():
		path_contract = openapi.get("paths", {}).get(endpoint, {})
		operation = path_contract.get(method, {})
		if set(path_contract) != {method}:
			violations.append(f"{endpoint} must expose only {method.upper()}")
		expected_role = (
			admin_role if endpoint.endswith(("domain", "review_master_data_request")) else requester_role
		)
		if operation.get("x-required-role") != expected_role:
			violations.append(f"{endpoint} has the wrong master data role contract")
		if method == "post":
			if operation.get("x-transaction-boundary") != "Single DB transaction":
				violations.append(f"{endpoint} must use one database transaction")
			if operation.get("x-idempotency") != "Required for write":
				violations.append(f"{endpoint} must require idempotency")
			parameters = operation.get("parameters", [])
			if not any(
				parameter.get("name") == "Idempotency-Key"
				or parameter.get("$ref") == "#/components/parameters/IdempotencyKey"
				for parameter in parameters
			):
				violations.append(f"{endpoint} must declare a required Idempotency-Key header")
		else:
			if operation.get("x-transaction-boundary") != "Read-only":
				violations.append(f"{endpoint} must remain read-only")
			if operation.get("x-idempotency") != "Read-only deterministic":
				violations.append(f"{endpoint} must remain deterministic")

	integration_text = integration_test_path.read_text(encoding="utf-8")
	for token in (
		"test_desk_permissions_limit_hrp_users_to_their_own_requests",
		"test_submit_and_review_enforce_maker_checker_without_mutating_item",
		"test_target_drift_unknown_field_and_link_are_rejected",
		"test_http_domain_draft_submit_and_query",
		"test_http_write_requires_idempotency_header_and_review_is_maker_checked",
	):
		if token not in integration_text:
			violations.append(f"master data integration tests are missing: {token}")
	return violations


def validate_external_code_mapping_contract(root: Path) -> list[str]:
	module_root = root / APP_NAME / "hrp_master_data"
	common_path = root / APP_NAME / "common" / "external_code_mapping.py"
	service_path = module_root / "services" / "external_code_mapping.py"
	permission_path = module_root / "permissions.py"
	api_path = root / APP_NAME / "api" / "v1" / "master_data.py"
	setup_path = root / APP_NAME / "setup" / "master_data.py"
	hooks_path = root / APP_NAME / "hooks.py"
	runtime_path = module_root / "doctype" / "hrp_external_code_mapping" / "hrp_external_code_mapping.json"
	controller_path = runtime_path.with_suffix(".py")
	blueprint_path = root / "doctype_blueprints" / "hrp_master_data" / "hrp_external_code_mapping.json"
	workspace_path = module_root / "workspace" / "hrp_master_data" / "hrp_master_data.json"
	root_workspace_path = root / APP_NAME / "hrp_foundation" / "workspace" / "hrp" / "hrp.json"
	documentation_path = root / "architecture" / "external_code_mapping.md"
	adr_path = root / "architecture" / "adr" / "ADR-0015-service-owned-bidirectional-external-code-mapping.md"
	task_path = root / "backlog" / "COD-021.md"
	change_path = root / "changes" / "COD-021.json"
	pure_test_path = root / "tests" / "test_external_code_mapping.py"
	integration_test_path = module_root / "tests" / "test_external_code_mapping.py"
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		common_path,
		service_path,
		permission_path,
		api_path,
		setup_path,
		hooks_path,
		runtime_path,
		controller_path,
		blueprint_path,
		workspace_path,
		root_workspace_path,
		documentation_path,
		adr_path,
		task_path,
		change_path,
		pure_test_path,
		integration_test_path,
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing external code mapping contract file: {path}" for path in missing]

	violations: list[str] = []
	expected_fields = {
		"master_data_domain",
		"target_doctype",
		"internal_name",
		"company",
		"hospital",
		"organization_unit",
		"scope_key",
		"source_key",
		"target_key",
		"external_system",
		"external_code",
		"external_label",
		"enabled",
		"valid_from",
		"valid_to",
		"revision",
		"remarks",
	}
	runtime_text = runtime_path.read_text(encoding="utf-8")
	blueprint_text = blueprint_path.read_text(encoding="utf-8")
	runtime = json.loads(runtime_text)
	blueprint = json.loads(blueprint_text)
	if "?" in blueprint_text:
		violations.append("external code mapping blueprint contains replacement question marks")
	for metadata, source in ((runtime, "runtime"), (blueprint, "blueprint")):
		if metadata.get("name") != EXTERNAL_CODE_MAPPING_DOCTYPE:
			violations.append(f"external code mapping {source} has the wrong DocType name")
		if metadata.get("module") != "HRP Master Data":
			violations.append(f"external code mapping {source} has the wrong module")
		fields = {field.get("fieldname") for field in metadata.get("fields", []) if field.get("fieldname")}
		if fields != expected_fields:
			violations.append(f"external code mapping {source} fields must match the contract")
	if runtime.get("autoname") != "hash" or runtime.get("allow_rename"):
		violations.append("external code mapping must have a stable hash name")
	if runtime.get("is_submittable"):
		violations.append("external code mapping must not be submittable")
	expected_roles = {
		"System Manager",
		"HRP System Manager",
		"HRP Data Steward",
		"HRP Integration User",
	}
	for metadata, source in ((runtime, "runtime"), (blueprint, "blueprint")):
		permissions = metadata.get("permissions", [])
		if {permission.get("role") for permission in permissions} != expected_roles:
			violations.append(f"external code mapping {source} has the wrong roles")
		for permission in permissions:
			if permission.get("read") != 1 or any(
				permission.get(action)
				for action in ("write", "create", "delete", "submit", "cancel", "amend")
			):
				violations.append(f"external code mapping {source} must remain read-only")
	for fieldname in ("scope_key", "source_key", "target_key"):
		field = next(
			(item for item in runtime.get("fields", []) if item.get("fieldname") == fieldname),
			{},
		)
		if not field.get("hidden") or not field.get("read_only") or not field.get("reqd"):
			violations.append(f"external code mapping {fieldname} must be hidden and read-only")

	common_text = common_path.read_text(encoding="utf-8")
	for token in (
		f"EXTERNAL_CODE_MAPPING_SCHEMA_VERSION = {EXTERNAL_CODE_MAPPING_SCHEMA_VERSION}",
		"class ExternalCodeMappingUpsert",
		"class ExternalCodeMappingResolve",
		"class InternalCodeMappingResolve",
		"def source_key_for",
		"def target_key_for",
		"medops-external-code-mapping-v1",
		"effective_on",
	):
		if token not in common_text:
			violations.append(f"external code mapping public contract is missing: {token}")

	controller_text = controller_path.read_text(encoding="utf-8")
	for token in (
		"external_code_mapping_service_write",
		"source_key_for",
		"target_key_for",
		"lock_revision",
		"as_public_dict",
		"OPERATION_NOT_ALLOWED",
	):
		if token not in controller_text:
			violations.append(f"external code mapping controller is missing: {token}")
	if "frappe.db.commit" in controller_text:
		violations.append("external code mapping controller must not commit transactions")

	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		"class UpsertExternalCodeMappingService",
		"class ResolveExternalCodeMappingService",
		"class ResolveInternalCodeMappingService",
		"EXTERNAL_CODE_MAPPING_WRITE_ROLES",
		"EXTERNAL_CODE_MAPPING_READ_ROLES",
		"FOR UPDATE",
		"_assert_hospital_scope",
		"_assert_internal_target",
		"_conflicting_mapping",
		'key_field not in {"source_key", "target_key"}',
	):
		if token not in service_text:
			violations.append(f"external code mapping service is missing: {token}")
	if "frappe.db.commit" in service_text:
		violations.append("external code mapping services must not commit transactions")
	for forbidden in (
		'frappe.get_doc("Department"',
		'frappe.get_doc("Cost Center"',
		'frappe.get_doc("Item"',
		'frappe.get_doc("Supplier"',
		'frappe.get_doc("Warehouse"',
		"frappe.db.set_value",
	):
		if forbidden in service_text:
			violations.append(f"external code mapping must not mutate a standard record: {forbidden}")

	permission_text = permission_path.read_text(encoding="utf-8")
	hooks_text = hooks_path.read_text(encoding="utf-8")
	for token in (
		"def external_code_mapping_query",
		"def can_read_external_code_mapping",
		"EXTERNAL_CODE_MAPPING_READ_ROLES",
	):
		if token not in permission_text:
			violations.append(f"external code mapping permission contract is missing: {token}")
	for token in (
		'"HRP External Code Mapping": "medops.hrp_master_data.permissions.external_code_mapping_query"',
		'"HRP External Code Mapping": "medops.hrp_master_data.permissions.can_read_external_code_mapping"',
	):
		if token not in hooks_text:
			violations.append(f"external code mapping permission hook is missing: {token}")

	api_text = api_path.read_text(encoding="utf-8")
	for method_name in (
		"upsert_external_code_mapping",
		"resolve_external_code_mapping",
		"resolve_internal_code_mapping",
	):
		if f"def {method_name}(" not in api_text:
			violations.append(f"external code mapping API is missing method: {method_name}")
	if "allow_guest=True" in api_text:
		violations.append("external code mapping APIs must never allow guests")

	setup_text = setup_path.read_text(encoding="utf-8")
	for token in (
		"uniq_hrp_external_code_source",
		"uniq_hrp_external_code_target",
		"idx_hrp_external_code_effectivity",
	):
		if token not in setup_text:
			violations.append(f"external code mapping migration is missing: {token}")

	workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
	root_workspace = json.loads(root_workspace_path.read_text(encoding="utf-8"))
	if EXTERNAL_CODE_MAPPING_DOCTYPE not in {link.get("link_to") for link in workspace.get("links", [])}:
		violations.append("master data workspace must expose external code mappings")
	if EXTERNAL_CODE_MAPPING_DOCTYPE not in {
		item.get("link_to") for item in root_workspace.get("sidebar_items", [])
	}:
		violations.append("HRP sidebar must expose external code mappings")

	with (root / "design" / "doctype_catalog.csv").open(
		encoding="utf-8-sig",
		newline="",
	) as stream:
		doctype_rows = [
			row for row in csv.DictReader(stream) if row.get("doctype") == EXTERNAL_CODE_MAPPING_DOCTYPE
		]
	if len(doctype_rows) != 1 or doctype_rows[0].get("kind") != "Master":
		violations.append("external code mapping DocType catalog row is invalid")
	with (root / "design" / "field_catalog.csv").open(
		encoding="utf-8-sig",
		newline="",
	) as stream:
		field_rows = [
			row for row in csv.DictReader(stream) if row.get("doctype") == EXTERNAL_CODE_MAPPING_DOCTYPE
		]
	if {row.get("fieldname") for row in field_rows} != expected_fields:
		violations.append("external code mapping field catalog must match runtime metadata")
	for key_field in ("source_key", "target_key"):
		row = next((item for item in field_rows if item.get("fieldname") == key_field), {})
		if row.get("unique") != "1" or row.get("read_only") != "1":
			violations.append(f"external code mapping {key_field} catalog contract is invalid")

	endpoints = {
		"/api/method/medops.api.v1.master_data.resolve_external_code_mapping": "get",
		"/api/method/medops.api.v1.master_data.upsert_external_code_mapping": "post",
		"/api/method/medops.api.v1.master_data.resolve_internal_code_mapping": "get",
	}
	for catalog_path in catalog_paths:
		catalog_text = catalog_path.read_text(encoding="utf-8")
		for endpoint in endpoints:
			if endpoint not in catalog_text:
				violations.append(
					f"{catalog_path.relative_to(root)} is missing external mapping endpoint {endpoint}"
				)
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	write_role = "System Manager or HRP System Manager or HRP Data Steward"
	read_role = f"{write_role} or HRP Integration User"
	for endpoint, method in endpoints.items():
		path_contract = openapi.get("paths", {}).get(endpoint, {})
		operation = path_contract.get(method, {})
		if set(path_contract) != {method}:
			violations.append(f"{endpoint} must expose only {method.upper()}")
		expected_role = write_role if method == "post" else read_role
		if operation.get("x-required-role") != expected_role:
			violations.append(f"{endpoint} has the wrong external mapping role contract")
		if method == "post":
			if operation.get("x-transaction-boundary") != "Single DB transaction":
				violations.append(f"{endpoint} must use one database transaction")
			if operation.get("x-idempotency") != "Required for write":
				violations.append(f"{endpoint} must require idempotency")
			if not any(
				parameter.get("$ref") == "#/components/parameters/IdempotencyKey"
				for parameter in operation.get("parameters", [])
			):
				violations.append(f"{endpoint} must declare Idempotency-Key")
		else:
			if operation.get("x-transaction-boundary") != "Read-only":
				violations.append(f"{endpoint} must remain read-only")
			if operation.get("x-idempotency") != "Read-only deterministic":
				violations.append(f"{endpoint} must remain deterministic")

	pure_test_text = pure_test_path.read_text(encoding="utf-8")
	integration_test_text = integration_test_path.read_text(encoding="utf-8")
	for token in (
		"test_source_and_target_keys_are_deterministic_and_directional",
		"test_bidirectional_resolvers_require_explicit_effective_date",
	):
		if token not in pure_test_text:
			violations.append(f"external code mapping pure tests are missing: {token}")
	for token in (
		"test_metadata_migration_workspace_and_service_only_write",
		"test_upsert_is_idempotent_revisioned_and_identity_is_immutable",
		"test_uniqueness_and_bidirectional_effectivity",
		"test_scope_target_and_role_checks_precede_idempotency",
		"test_permissions_and_audit_are_redacted",
		"test_http_write_requires_idempotency_header",
		"test_http_upsert_and_bidirectional_queries",
	):
		if token not in integration_test_text:
			violations.append(f"external code mapping integration tests are missing: {token}")
	return violations


def validate_data_quality_contract(root: Path) -> list[str]:
	module_root = root / APP_NAME / "hrp_master_data"
	common_path = root / APP_NAME / "common" / "data_quality.py"
	service_path = module_root / "services" / "data_quality.py"
	permission_path = module_root / "permissions.py"
	api_path = root / APP_NAME / "api" / "v1" / "master_data.py"
	setup_path = root / APP_NAME / "setup" / "master_data.py"
	hooks_path = root / APP_NAME / "hooks.py"
	doctypes = {
		DATA_QUALITY_RULE_DOCTYPE: "hrp_data_quality_rule",
		DATA_QUALITY_ISSUE_DOCTYPE: "hrp_data_quality_issue",
	}
	runtime_paths = {
		name: module_root / "doctype" / directory / f"{directory}.json"
		for name, directory in doctypes.items()
	}
	controller_paths = {
		name: module_root / "doctype" / directory / f"{directory}.py" for name, directory in doctypes.items()
	}
	blueprint_paths = {
		name: root / "doctype_blueprints" / "hrp_master_data" / f"{directory}.json"
		for name, directory in doctypes.items()
	}
	workspace_path = module_root / "workspace" / "hrp_master_data" / "hrp_master_data.json"
	root_workspace_path = root / APP_NAME / "hrp_foundation" / "workspace" / "hrp" / "hrp.json"
	documentation_path = root / "architecture" / "data_quality_rules.md"
	adr_path = root / "architecture" / "adr" / "ADR-0016-declarative-master-data-quality-rules.md"
	task_path = root / "backlog" / "COD-022.md"
	change_path = root / "changes" / "COD-022.json"
	pure_test_path = root / "tests" / "test_data_quality.py"
	integration_test_path = module_root / "tests" / "test_data_quality.py"
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		common_path,
		service_path,
		permission_path,
		api_path,
		setup_path,
		hooks_path,
		workspace_path,
		root_workspace_path,
		documentation_path,
		adr_path,
		task_path,
		change_path,
		pure_test_path,
		integration_test_path,
		*runtime_paths.values(),
		*controller_paths.values(),
		*blueprint_paths.values(),
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing data quality contract file: {path}" for path in missing]

	violations: list[str] = []
	expected_fields = {
		DATA_QUALITY_RULE_DOCTYPE: {
			"master_data_domain",
			"target_doctype",
			"code",
			"display_name",
			"company",
			"hospital",
			"organization_unit",
			"target_field",
			"rule_type",
			"parameters_json",
			"severity",
			"enabled",
			"valid_from",
			"valid_to",
			"rule_digest",
			"revision",
			"remarks",
		},
		DATA_QUALITY_ISSUE_DOCTYPE: {
			"quality_rule",
			"master_data_domain",
			"target_doctype",
			"target_name",
			"company",
			"hospital",
			"organization_unit",
			"issue_key",
			"issue_status",
			"severity",
			"failure_code",
			"failure_message",
			"observed_value_digest",
			"rule_revision",
			"rule_digest",
			"first_detected_at",
			"last_evaluated_at",
			"resolved_at",
			"occurrence_count",
			"revision",
		},
	}
	expected_roles = {"System Manager", "HRP System Manager", "HRP Data Steward"}
	for name in doctypes:
		runtime_text = runtime_paths[name].read_text(encoding="utf-8")
		blueprint_text = blueprint_paths[name].read_text(encoding="utf-8")
		runtime = json.loads(runtime_text)
		blueprint = json.loads(blueprint_text)
		if "?" in blueprint_text:
			violations.append(f"{name} blueprint contains replacement question marks")
		for metadata, source in ((runtime, "runtime"), (blueprint, "blueprint")):
			if metadata.get("name") != name or metadata.get("module") != "HRP Master Data":
				violations.append(f"{name} {source} has the wrong owner")
			fields = {
				field.get("fieldname") for field in metadata.get("fields", []) if field.get("fieldname")
			}
			if fields != expected_fields[name]:
				violations.append(f"{name} {source} fields must match the contract")
			permissions = metadata.get("permissions", [])
			if {permission.get("role") for permission in permissions} != expected_roles:
				violations.append(f"{name} {source} has the wrong roles")
			for permission in permissions:
				if permission.get("read") != 1 or any(
					permission.get(action)
					for action in ("write", "create", "delete", "submit", "cancel", "amend")
				):
					violations.append(f"{name} {source} must remain read-only")
		if runtime.get("is_submittable") or runtime.get("allow_rename"):
			violations.append(f"{name} must be non-submittable and non-renamable")
	if (
		json.loads(runtime_paths[DATA_QUALITY_RULE_DOCTYPE].read_text(encoding="utf-8")).get("autoname")
		!= "field:code"
	):
		violations.append("data quality rule must use its stable code as name")
	if (
		json.loads(runtime_paths[DATA_QUALITY_ISSUE_DOCTYPE].read_text(encoding="utf-8")).get("autoname")
		!= "hash"
	):
		violations.append("data quality issue must use a stable hash name")

	common_text = common_path.read_text(encoding="utf-8")
	for token in (
		f"DATA_QUALITY_SCHEMA_VERSION = {DATA_QUALITY_SCHEMA_VERSION}",
		"MAX_RULE_PARAMETERS_BYTES = 8 * 1024",
		"MAX_ALLOWED_VALUES = 64",
		"MAX_TEXT_RULE_LENGTH = 500",
		"class DataQualityRuleUpsert",
		"class DataQualityEvaluate",
		"def evaluate_quality_value",
		"def issue_key_for",
		"def observed_value_digest",
		"SENSITIVE_FIELD_TOKENS",
	):
		if token not in common_text:
			violations.append(f"data quality public contract is missing: {token}")
	for rule_type in RULE_TYPES:
		if f'"{rule_type}"' not in common_text:
			violations.append(f"data quality rule type is missing: {rule_type}")
	for pattern_name in NAMED_PATTERNS:
		if f'"{pattern_name}"' not in common_text:
			violations.append(f"data quality named pattern is missing: {pattern_name}")
	for forbidden in ("eval(", "exec(", "ast.literal_eval", "parameters_sql", "custom_regex"):
		if forbidden in common_text:
			violations.append(f"data quality rules must not execute user code: {forbidden}")

	for name, controller_path in controller_paths.items():
		controller_text = controller_path.read_text(encoding="utf-8")
		for token in ("data_quality_service_write", "as_public_dict", "OPERATION_NOT_ALLOWED"):
			if token not in controller_text:
				violations.append(f"{name} controller is missing: {token}")
		if "frappe.db.commit" in controller_text:
			violations.append(f"{name} controller must not commit transactions")

	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		"class UpsertDataQualityRuleService",
		"class EvaluateDataQualityService",
		"DATA_QUALITY_BATCH_SIZE = 200",
		"DomainService",
		"FOR UPDATE",
		"expected_rule_revision",
		"issue_key_for",
		"observed_value_digest",
		"enqueue_with_audit",
		"run_data_quality_rule_batch",
		"run_data_quality_rules",
	):
		if token not in service_text:
			violations.append(f"data quality service is missing: {token}")
	if "frappe.db.commit" in service_text:
		violations.append("data quality services must not commit transactions")
	for forbidden in (
		'frappe.get_doc("Department"',
		'frappe.get_doc("Cost Center"',
		'frappe.get_doc("Item"',
		'frappe.get_doc("Supplier"',
		'frappe.get_doc("Warehouse"',
		"frappe.db.set_value",
	):
		if forbidden in service_text:
			violations.append(f"data quality must not mutate a standard record: {forbidden}")

	permission_text = permission_path.read_text(encoding="utf-8")
	hooks_text = hooks_path.read_text(encoding="utf-8")
	for token in ("def data_quality_query", "def can_read_data_quality", "DATA_QUALITY_READ_ROLES"):
		if token not in permission_text:
			violations.append(f"data quality permission contract is missing: {token}")
	for name in doctypes:
		for token in (
			f'"{name}": "medops.hrp_master_data.permissions.data_quality_query"',
			f'"{name}": "medops.hrp_master_data.permissions.can_read_data_quality"',
		):
			if token not in hooks_text:
				violations.append(f"data quality permission hook is missing: {token}")
	if "medops.hrp_master_data.services.data_quality.run_data_quality_rules" not in hooks_text:
		violations.append("daily data quality scheduler hook is missing")

	api_text = api_path.read_text(encoding="utf-8")
	for method_name in (
		"upsert_data_quality_rule",
		"evaluate_data_quality",
		"get_data_quality_issue",
	):
		if f"def {method_name}(" not in api_text:
			violations.append(f"data quality API is missing method: {method_name}")
	if "allow_guest=True" in api_text:
		violations.append("data quality APIs must never allow guests")

	setup_text = setup_path.read_text(encoding="utf-8")
	for token in (
		"idx_hrp_data_quality_rule_schedule",
		"uniq_hrp_data_quality_issue_key",
		"idx_hrp_data_quality_issue_status",
	):
		if token not in setup_text:
			violations.append(f"data quality migration is missing: {token}")

	workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
	root_workspace = json.loads(root_workspace_path.read_text(encoding="utf-8"))
	workspace_links = {link.get("link_to") for link in workspace.get("links", [])}
	root_links = {item.get("link_to") for item in root_workspace.get("sidebar_items", [])}
	if not set(doctypes).issubset(workspace_links):
		violations.append("master data workspace must expose data quality rules and issues")
	if not set(doctypes).issubset(root_links):
		violations.append("HRP sidebar must expose data quality rules and issues")

	with (root / "design" / "doctype_catalog.csv").open(
		encoding="utf-8-sig",
		newline="",
	) as stream:
		doctype_rows = [row for row in csv.DictReader(stream) if row.get("doctype") in doctypes]
	if {row.get("doctype") for row in doctype_rows} != set(doctypes):
		violations.append("data quality DocType catalog rows are missing")
	for row in doctype_rows:
		if row.get("is_submittable") != "0" or row.get("allow_rename") != "0":
			violations.append(f"{row.get('doctype')} catalog lifecycle is invalid")
	with (root / "design" / "field_catalog.csv").open(
		encoding="utf-8-sig",
		newline="",
	) as stream:
		field_rows = [row for row in csv.DictReader(stream) if row.get("doctype") in doctypes]
	for name, fields in expected_fields.items():
		if {row.get("fieldname") for row in field_rows if row.get("doctype") == name} != fields:
			violations.append(f"{name} field catalog must match runtime metadata")

	endpoints = {
		"/api/method/medops.api.v1.master_data.upsert_data_quality_rule": "post",
		"/api/method/medops.api.v1.master_data.evaluate_data_quality": "post",
		"/api/method/medops.api.v1.master_data.get_data_quality_issue": "get",
	}
	for catalog_path in catalog_paths:
		catalog_text = catalog_path.read_text(encoding="utf-8")
		for endpoint in endpoints:
			if endpoint not in catalog_text:
				violations.append(
					f"{catalog_path.relative_to(root)} is missing data quality endpoint {endpoint}"
				)
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	required_role = "System Manager or HRP System Manager or HRP Data Steward"
	for endpoint, method in endpoints.items():
		path_contract = openapi.get("paths", {}).get(endpoint, {})
		operation = path_contract.get(method, {})
		if set(path_contract) != {method}:
			violations.append(f"{endpoint} must expose only {method.upper()}")
		if operation.get("x-required-role") != required_role:
			violations.append(f"{endpoint} has the wrong data quality role contract")
		if method == "post":
			if operation.get("x-transaction-boundary") != "Single DB transaction":
				violations.append(f"{endpoint} must use one database transaction")
			if operation.get("x-idempotency") != "Required for write":
				violations.append(f"{endpoint} must require idempotency")
			if not any(
				parameter.get("$ref") == "#/components/parameters/IdempotencyKey"
				for parameter in operation.get("parameters", [])
			):
				violations.append(f"{endpoint} must declare Idempotency-Key")
		else:
			if operation.get("x-transaction-boundary") != "Read-only":
				violations.append(f"{endpoint} must remain read-only")
			if operation.get("x-idempotency") != "Read-only deterministic":
				violations.append(f"{endpoint} must remain deterministic")

	pure_test_text = pure_test_path.read_text(encoding="utf-8")
	integration_test_text = integration_test_path.read_text(encoding="utf-8")
	for token in (
		"test_rule_parameters_reject_executable_or_ambiguous_content",
		"test_declarative_evaluator_covers_all_rule_types",
		"test_issue_and_observed_digests_are_deterministic_and_directional",
	):
		if token not in pure_test_text:
			violations.append(f"data quality pure tests are missing: {token}")
	for token in (
		"test_metadata_migration_workspace_and_service_only_write",
		"test_upsert_is_idempotent_revisioned_and_identity_is_immutable",
		"test_failure_creates_issue_then_resolves_and_reopens",
		"test_scope_role_and_unknown_target_checks_precede_idempotency",
		"test_database_issue_uniqueness_race_is_reported_as_conflict",
		"test_permissions_and_audit_are_redacted",
		"test_scheduler_batches_rules_and_targets_without_unbounded_work",
		"test_http_write_requires_idempotency_header",
		"test_http_rule_evaluation_and_issue_query",
	):
		if token not in integration_test_text:
			violations.append(f"data quality integration tests are missing: {token}")
	return violations


def validate_numbering_contract(root: Path) -> list[str]:
	module_root = root / APP_NAME / "hrp_foundation"
	common_path = root / APP_NAME / "common" / "numbering.py"
	service_path = module_root / "services" / "numbering.py"
	permission_path = module_root / "permissions.py"
	api_path = root / APP_NAME / "api" / "v1" / "core" / "numbering.py"
	setup_path = root / APP_NAME / "setup" / "numbering.py"
	install_path = root / APP_NAME / "setup" / "install.py"
	hooks_path = root / APP_NAME / "hooks.py"
	doctypes = {
		NUMBERING_SCHEME_DOCTYPE: "hrp_numbering_scheme",
		NUMBER_RESERVATION_DOCTYPE: "hrp_number_reservation",
	}
	runtime_paths = {
		name: module_root / "doctype" / directory / f"{directory}.json"
		for name, directory in doctypes.items()
	}
	controller_paths = {
		name: module_root / "doctype" / directory / f"{directory}.py" for name, directory in doctypes.items()
	}
	blueprint_paths = {
		name: root / "doctype_blueprints" / "hrp_foundation" / f"{directory}.json"
		for name, directory in doctypes.items()
	}
	workspace_path = module_root / "workspace" / "hrp" / "hrp.json"
	documentation_path = root / "architecture" / "unified_numbering.md"
	adr_path = root / "architecture" / "adr" / "ADR-0017-atomic-unified-number-reservations.md"
	task_path = root / "backlog" / "COD-023.md"
	change_path = root / "changes" / "COD-023.json"
	pure_test_path = root / "tests" / "test_numbering.py"
	integration_test_path = module_root / "tests" / "test_numbering.py"
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		common_path,
		service_path,
		permission_path,
		api_path,
		setup_path,
		install_path,
		hooks_path,
		workspace_path,
		documentation_path,
		adr_path,
		task_path,
		change_path,
		pure_test_path,
		integration_test_path,
		*runtime_paths.values(),
		*controller_paths.values(),
		*blueprint_paths.values(),
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing numbering contract file: {path}" for path in missing]

	violations: list[str] = []
	expected_fields = {
		NUMBERING_SCHEME_DOCTYPE: {
			"code",
			"display_name",
			"company",
			"hospital",
			"organization_unit",
			"enabled",
			"template",
			"dimension_keys",
			"reset_policy",
			"sequence_digits",
			"start_number",
			"template_digest",
			"revision",
			"valid_from",
			"valid_to",
			"remarks",
		},
		NUMBER_RESERVATION_DOCTYPE: {
			"numbering_scheme",
			"scheme_code",
			"number",
			"reservation_token",
			"business_date",
			"company",
			"hospital",
			"organization_unit",
			"dimensions_json",
			"dimensions_digest",
			"reset_bucket",
			"counter_key",
			"sequence_value",
			"scheme_revision",
			"template_digest",
			"allocated_by",
			"correlation_id",
			"request_id",
		},
	}
	expected_roles = {
		"System Manager",
		"HRP System Manager",
		"HRP User",
		"HRP Auditor",
		"HRP Integration User",
	}
	for name in doctypes:
		runtime_text = runtime_paths[name].read_text(encoding="utf-8")
		blueprint_text = blueprint_paths[name].read_text(encoding="utf-8")
		for metadata, source in (
			(json.loads(runtime_text), "runtime"),
			(json.loads(blueprint_text), "blueprint"),
		):
			if metadata.get("name") != name or metadata.get("module") != "HRP Foundation":
				violations.append(f"{name} {source} has the wrong owner")
			fields = {
				field.get("fieldname")
				for field in metadata.get("fields", [])
				if field.get("fieldname") and not str(field.get("fieldtype", "")).endswith("Break")
			}
			if fields != expected_fields[name]:
				violations.append(f"{name} {source} fields must match the contract")
			permissions = metadata.get("permissions", [])
			if {permission.get("role") for permission in permissions} != expected_roles:
				violations.append(f"{name} {source} has the wrong roles")
			for permission in permissions:
				if permission.get("read") != 1 or any(
					permission.get(action)
					for action in ("write", "create", "delete", "submit", "cancel", "amend")
				):
					violations.append(f"{name} {source} must remain read-only")
		if "?" in blueprint_text:
			violations.append(f"{name} blueprint contains replacement question marks")
		runtime = json.loads(runtime_text)
		if runtime.get("is_submittable") or runtime.get("allow_rename"):
			violations.append(f"{name} must be non-submittable and non-renamable")
	if (
		json.loads(runtime_paths[NUMBERING_SCHEME_DOCTYPE].read_text(encoding="utf-8")).get("autoname")
		!= "field:code"
	):
		violations.append("numbering scheme must use its stable code as name")
	if (
		json.loads(runtime_paths[NUMBER_RESERVATION_DOCTYPE].read_text(encoding="utf-8")).get("autoname")
		!= "field:reservation_token"
	):
		violations.append("number reservation must use its token as name")

	common_text = common_path.read_text(encoding="utf-8")
	for token in (
		f"NUMBERING_SCHEMA_VERSION = {NUMBERING_SCHEMA_VERSION}",
		f"MAX_TEMPLATE_LENGTH = {MAX_TEMPLATE_LENGTH}",
		f"MAX_DIMENSIONS = {MAX_DIMENSIONS}",
		f"MIN_SEQUENCE_DIGITS = {MIN_SEQUENCE_DIGITS}",
		f"MAX_SEQUENCE_DIGITS = {MAX_SEQUENCE_DIGITS}",
		"class NumberingSchemeUpsert",
		"class NumberAllocation",
		"def counter_key_for",
		"def render_number",
		"def reservation_name_for",
	):
		if token not in common_text:
			violations.append(f"numbering public contract is missing: {token}")
	for policy in RESET_POLICIES:
		if f'"{policy}"' not in common_text:
			violations.append(f"numbering reset policy is missing: {policy}")
	for forbidden in ("eval(", "exec(", "ast.literal_eval", "custom_sql", "custom_expression"):
		if forbidden in common_text:
			violations.append(f"numbering templates must not execute user code: {forbidden}")

	for name, controller_path in controller_paths.items():
		controller_text = controller_path.read_text(encoding="utf-8")
		for token in ("numbering_service_write", "as_public_dict", "OPERATION_NOT_ALLOWED"):
			if token not in controller_text:
				violations.append(f"{name} controller is missing: {token}")
		if "frappe.db.commit" in controller_text:
			violations.append(f"{name} controller must not commit transactions")

	service_text = service_path.read_text(encoding="utf-8")
	for token in (
		"class UpsertNumberingSchemeService",
		"class AllocateNumberService",
		"DomainService",
		"INSERT INTO `tabSeries`",
		"ON DUPLICATE KEY UPDATE",
		"FOR UPDATE",
		"HRP Number Reservation",
		"frappe.generate_hash",
		"dimensions_digest",
		"template_digest",
	):
		if token not in service_text:
			violations.append(f"numbering service is missing: {token}")
	if "frappe.db.commit" in service_text:
		violations.append("numbering services must not commit transactions")
	update_branch = service_text.partition("name = command.scheme_name or command.code")[2].partition(
		"class AllocateNumberService"
	)[0]
	revision_lock = update_branch.find("HRPNumberingScheme.lock_revision")
	scope_lock = update_branch.find("_assert_scope(")
	if revision_lock < 0 or scope_lock < 0 or revision_lock > scope_lock:
		violations.append("numbering scheme updates must lock scheme before organization scope")

	permission_text = permission_path.read_text(encoding="utf-8")
	hooks_text = hooks_path.read_text(encoding="utf-8")
	for token in (
		"def numbering_scheme_query",
		"def number_reservation_query",
		"def can_read_numbering_scheme",
		"def can_read_number_reservation",
	):
		if token not in permission_text:
			violations.append(f"numbering permission contract is missing: {token}")
	for name, query, permission in (
		(NUMBERING_SCHEME_DOCTYPE, "numbering_scheme_query", "can_read_numbering_scheme"),
		(NUMBER_RESERVATION_DOCTYPE, "number_reservation_query", "can_read_number_reservation"),
	):
		for token in (
			f'"{name}": "medops.hrp_foundation.permissions.{query}"',
			f'"{name}": "medops.hrp_foundation.permissions.{permission}"',
		):
			if token not in hooks_text:
				violations.append(f"numbering permission hook is missing: {token}")

	api_text = api_path.read_text(encoding="utf-8")
	for method_name, http_method in (
		("upsert_scheme", "POST"),
		("next", "POST"),
		("get_reservation", "GET"),
	):
		if f"def {method_name}(" not in api_text:
			violations.append(f"numbering API is missing method: {method_name}")
		if f'@frappe.whitelist(allow_guest=True, methods=["{http_method}"])' not in api_text:
			violations.append(f"numbering API must expose controlled guest boundary: {method_name}")
	if api_text.count("require_authenticated_user()") != 3:
		violations.append("numbering APIs must authenticate before parsing or service access")

	setup_text = setup_path.read_text(encoding="utf-8")
	for token in (
		"uniq_hrp_number_reservation_number",
		"uniq_hrp_number_reservation_token",
		"idx_hrp_number_reservation_scheme_date",
		"idx_hrp_number_reservation_owner",
		"idx_hrp_numbering_scheme_effectivity",
	):
		if token not in setup_text:
			violations.append(f"numbering migration is missing: {token}")
	if "ensure_numbering_governance()" not in install_path.read_text(encoding="utf-8"):
		violations.append("numbering migration must run after install and migrate")

	workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
	shortcut_links = {item.get("link_to") for item in workspace.get("shortcuts", [])}
	sidebar_links = {item.get("link_to") for item in workspace.get("sidebar_items", [])}
	if not set(doctypes).issubset(shortcut_links):
		violations.append("HRP workspace must expose numbering shortcuts")
	if not set(doctypes).issubset(sidebar_links):
		violations.append("HRP sidebar must expose numbering DocTypes")

	with (root / "design" / "doctype_catalog.csv").open(
		encoding="utf-8-sig",
		newline="",
	) as stream:
		doctype_rows = [row for row in csv.DictReader(stream) if row.get("doctype") in doctypes]
	if {row.get("doctype") for row in doctype_rows} != set(doctypes):
		violations.append("numbering DocType catalog rows are missing")
	for row in doctype_rows:
		if row.get("is_submittable") != "0" or row.get("allow_rename") != "0":
			violations.append(f"{row.get('doctype')} catalog lifecycle is invalid")
	with (root / "design" / "field_catalog.csv").open(
		encoding="utf-8-sig",
		newline="",
	) as stream:
		field_rows = [row for row in csv.DictReader(stream) if row.get("doctype") in doctypes]
	for name, fields in expected_fields.items():
		if {row.get("fieldname") for row in field_rows if row.get("doctype") == name} != fields:
			violations.append(f"{name} field catalog must match runtime metadata")

	endpoints = {
		"/api/method/medops.api.v1.core.numbering.next": (
			"post",
			"System Manager or HRP System Manager or HRP User or HRP Integration User",
			"Required for write",
		),
		"/api/method/medops.api.v1.core.numbering.upsert_scheme": (
			"post",
			"System Manager or HRP System Manager",
			"Required for write",
		),
		"/api/method/medops.api.v1.core.numbering.get_reservation": (
			"get",
			"System Manager or HRP System Manager or HRP User or HRP Auditor or HRP Integration User",
			"Read-only owner scoped",
		),
	}
	for catalog_path in catalog_paths:
		catalog_text = catalog_path.read_text(encoding="utf-8")
		for endpoint in endpoints:
			if endpoint not in catalog_text:
				violations.append(
					f"{catalog_path.relative_to(root)} is missing numbering endpoint {endpoint}"
				)
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	for endpoint, (method, role, idempotency) in endpoints.items():
		path_contract = openapi.get("paths", {}).get(endpoint, {})
		operation = path_contract.get(method, {})
		if set(path_contract) != {method}:
			violations.append(f"{endpoint} must expose only {method.upper()}")
		if operation.get("x-required-role") != role:
			violations.append(f"{endpoint} has the wrong numbering role contract")
		if operation.get("x-idempotency") != idempotency:
			violations.append(f"{endpoint} has the wrong idempotency contract")
		if method == "post":
			if operation.get("x-transaction-boundary") != "Single DB transaction":
				violations.append(f"{endpoint} must use one database transaction")
			if not any(
				parameter.get("$ref") == "#/components/parameters/IdempotencyKey"
				for parameter in operation.get("parameters", [])
			):
				violations.append(f"{endpoint} must declare Idempotency-Key")
		elif operation.get("x-transaction-boundary") != "Read-only":
			violations.append(f"{endpoint} must remain read-only")

	pure_test_text = pure_test_path.read_text(encoding="utf-8")
	integration_test_text = integration_test_path.read_text(encoding="utf-8")
	for token in (
		"test_template_rejects_expression_and_lowercase_literal",
		"test_dimensions_reject_non_scalar_and_injection_values",
		"test_counter_key_is_stable_and_scope_specific",
		"test_render_number_rejects_exhausted_sequence",
	):
		if token not in pure_test_text:
			violations.append(f"numbering pure tests are missing: {token}")
	for token in (
		"test_metadata_migration_workspace_and_service_only_write",
		"test_scheme_is_idempotent_revisioned_and_format_locks_after_allocation",
		"test_concurrent_scheme_identity_conflict_is_a_controlled_conflict",
		"test_allocation_is_atomic_idempotent_and_globally_unique",
		"test_dimension_and_reset_buckets_have_independent_sequences",
		"test_cross_scheme_number_collision_rolls_back_counter_and_idempotency",
		"test_reservation_is_immutable_and_permissions_limit_user_rows",
		"test_audit_contains_digests_without_number_or_dimensions",
		"test_http_write_requires_idempotency_header",
		"test_http_create_allocate_replay_and_query",
		"test_http_guest_is_rejected_before_numbering_lookup",
	):
		if token not in integration_test_text:
			violations.append(f"numbering integration tests are missing: {token}")
	return violations


def validate_access_scope_contract(root: Path) -> list[str]:
	module_root = root / APP_NAME / "hrp_workflow_authorization"
	runtime_paths = {
		ACCESS_SCOPE_DOCTYPE: module_root / "doctype" / "hrp_access_scope" / "hrp_access_scope.json",
		ACCESS_SCOPE_MEMBER_DOCTYPE: (
			module_root / "doctype" / "hrp_access_scope_member" / "hrp_access_scope_member.json"
		),
	}
	blueprint_paths = {
		name: root / "doctype_blueprints" / "hrp_workflow_authorization" / f"{directory}.json"
		for name, directory in (
			(ACCESS_SCOPE_DOCTYPE, "hrp_access_scope"),
			(ACCESS_SCOPE_MEMBER_DOCTYPE, "hrp_access_scope_member"),
		)
	}
	required_paths = (
		root / APP_NAME / "common" / "access_scope.py",
		module_root / "services" / "access_scope.py",
		module_root / "permissions.py",
		root / APP_NAME / "api" / "v1" / "core" / "scope.py",
		root / APP_NAME / "setup" / "access_scope.py",
		root / APP_NAME / "setup" / "workspaces.py",
		root / "architecture" / "access_scope.md",
		root / "tests" / "test_access_scope.py",
		module_root / "tests" / "test_access_scope.py",
		*runtime_paths.values(),
		*blueprint_paths.values(),
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing access scope contract file: {path}" for path in missing]

	violations: list[str] = []
	expected_fields = {
		ACCESS_SCOPE_DOCTYPE: {
			"code",
			"display_name",
			"scope_level",
			"enabled",
			"company",
			"hospital",
			"organization_unit",
			"include_descendants",
			"valid_from",
			"valid_to",
			"members",
			"revision",
			"policy_digest",
			"remarks",
		},
		ACCESS_SCOPE_MEMBER_DOCTYPE: {
			"user",
			"target_doctype",
			"allow_read",
			"allow_create",
			"allow_write",
			"allow_submit",
			"allow_cancel",
			"allow_export",
			"enabled",
		},
	}
	for name in runtime_paths:
		for metadata, source in (
			(json.loads(runtime_paths[name].read_text(encoding="utf-8")), "runtime"),
			(json.loads(blueprint_paths[name].read_text(encoding="utf-8")), "blueprint"),
		):
			fields = {
				field["fieldname"]
				for field in metadata.get("fields", [])
				if field.get("fieldname") and field.get("fieldtype") not in {"Section Break", "Column Break"}
			}
			if metadata.get("name") != name or metadata.get("module") != "HRP Workflow Authorization":
				violations.append(f"{name} {source} has the wrong owner")
			if fields != expected_fields[name]:
				violations.append(f"{name} {source} fields must match the access-scope contract")
		if "?" in blueprint_paths[name].read_text(encoding="utf-8"):
			violations.append(f"{name} blueprint contains replacement question marks")

	common_text = (root / APP_NAME / "common" / "access_scope.py").read_text(encoding="utf-8")
	for token in (
		f"ACCESS_SCOPE_SCHEMA_VERSION = {ACCESS_SCOPE_SCHEMA_VERSION}",
		"build_access_scope_definition",
		"build_scope_decision",
		"fail_closed",
		"decision_digest",
	):
		if token not in common_text:
			violations.append(f"access scope public contract is missing: {token}")
	for level in ACCESS_SCOPE_LEVELS:
		if level not in common_text:
			violations.append(f"access scope level is missing: {level}")
	for action in ACCESS_ACTIONS:
		if f'"{action}"' not in common_text:
			violations.append(f"access scope action is missing: {action}")
	for forbidden in ("eval(", "exec(", "frappe.safe_eval", "server_script"):
		if forbidden in common_text:
			violations.append(f"access scope contract must not execute user code: {forbidden}")

	service_text = (module_root / "services" / "access_scope.py").read_text(encoding="utf-8")
	for token in (
		"def resolve_access_scope",
		"ACCESS_SCOPE_ADMIN_ROLES",
		"frappe.has_permission",
		"_expand_organization_units",
		"access_scope_resolved",
	):
		if token not in service_text:
			violations.append(f"access scope service is missing: {token}")
	if "frappe.db.commit(" in service_text:
		violations.append("access scope service must remain read-only")

	hooks_text = (root / APP_NAME / "hooks.py").read_text(encoding="utf-8")
	permissions_text = (module_root / "permissions.py").read_text(encoding="utf-8")
	for doctype, hook in (
		("HRP Hospital", "hospital_query"),
		("HRP Organization Version", "organization_version_query"),
		("HRP Organization Unit", "organization_unit_query"),
		("HRP Organization Mapping", "organization_mapping_query"),
	):
		if doctype not in hooks_text or hook not in hooks_text:
			violations.append(f"access scope permission hook is missing: {doctype}")
	for token in ("scope_permission_query", "has_scoped_permission", "frappe.db.escape"):
		if token not in permissions_text:
			violations.append(f"access scope permission helper is missing: {token}")

	api_text = (root / APP_NAME / "api" / "v1" / "core" / "scope.py").read_text(encoding="utf-8")
	if 'methods=["POST"]' not in api_text or "resolve_access_scope" not in api_text:
		violations.append("CORE-001 must expose the reviewed read-only resolver")
	endpoint = "/api/method/medops.api.v1.core.scope.resolve"
	with (root / "api" / "api_catalog.csv").open(encoding="utf-8-sig", newline="") as handle:
		catalog_rows = {row["path"]: row for row in csv.DictReader(handle)}
	row = catalog_rows.get(endpoint)
	if not row or row.get("api_code") != "CORE-001" or row.get("idempotency") != "Not required (read-only)":
		violations.append("CORE-001 API catalog contract is invalid")
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	operation = openapi.get("paths", {}).get(endpoint, {}).get("post", {})
	if operation.get("x-transaction-boundary") != "Read-only request":
		violations.append("CORE-001 OpenAPI operation must remain read-only")
	if any(parameter.get("name") == "Idempotency-Key" for parameter in operation.get("parameters", [])):
		violations.append("CORE-001 must not require an idempotency key")

	pure_tests = (root / "tests" / "test_access_scope.py").read_text(encoding="utf-8")
	integration_tests = (module_root / "tests" / "test_access_scope.py").read_text(encoding="utf-8")
	for token in (
		"test_member_identity_is_unique_and_digest_is_order_independent",
		"test_filter_groups_use_only_reviewed_dimension_fields",
		"test_decision_fails_closed_without_scope_or_base_permission",
	):
		if token not in pure_tests:
			violations.append(f"access scope pure tests are missing: {token}")
	for token in (
		"test_metadata_indexes_and_owned_workspaces_are_idempotent",
		"test_resolver_expands_descendants_and_respects_target_doctype",
		"test_non_admin_cannot_resolve_another_user",
		"test_permission_helpers_filter_lists_and_documents",
		"test_http_resolve_is_read_only_and_needs_no_idempotency_key",
	):
		if token not in integration_tests:
			violations.append(f"access scope integration tests are missing: {token}")
	return violations


def validate_approval_matrix_contract(root: Path) -> list[str]:
	module_root = root / APP_NAME / "hrp_workflow_authorization"
	runtime_paths = {
		APPROVAL_MATRIX_DOCTYPE: (
			module_root / "doctype" / "hrp_approval_matrix" / "hrp_approval_matrix.json"
		),
		APPROVAL_MATRIX_ROW_DOCTYPE: (
			module_root / "doctype" / "hrp_approval_matrix_row" / "hrp_approval_matrix_row.json"
		),
	}
	blueprint_paths = {
		name: root / "doctype_blueprints" / "hrp_workflow_authorization" / f"{directory}.json"
		for name, directory in (
			(APPROVAL_MATRIX_DOCTYPE, "hrp_approval_matrix"),
			(APPROVAL_MATRIX_ROW_DOCTYPE, "hrp_approval_matrix_row"),
		)
	}
	required_paths = (
		root / APP_NAME / "common" / "approval_matrix.py",
		module_root / "services" / "approval_matrix.py",
		module_root / "permissions.py",
		root / APP_NAME / "api" / "v1" / "core" / "approval.py",
		root / APP_NAME / "setup" / "approval_matrix.py",
		root / "architecture" / "approval_matrix.md",
		root / "architecture" / "adr" / "ADR-0019-declarative-deterministic-approval-matrices.md",
		root / "tests" / "test_approval_matrix.py",
		module_root / "tests" / "test_approval_matrix.py",
		*runtime_paths.values(),
		*blueprint_paths.values(),
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing approval matrix contract file: {path}" for path in missing]

	violations: list[str] = []
	expected_fields = {
		APPROVAL_MATRIX_DOCTYPE: {
			"code",
			"display_name",
			"target_doctype",
			"enabled",
			"company",
			"hospital",
			"organization_unit",
			"include_descendants",
			"priority",
			"valid_from",
			"valid_to",
			"dimensions_json",
			"steps",
			"revision",
			"policy_digest",
			"remarks",
		},
		APPROVAL_MATRIX_ROW_DOCTYPE: {
			"sequence_no",
			"step_name",
			"threshold_amount",
			"approver_type",
			"approver_role",
			"approver_user",
			"approval_mode",
			"description",
		},
	}
	for name in runtime_paths:
		for metadata, source in (
			(json.loads(runtime_paths[name].read_text(encoding="utf-8")), "runtime"),
			(json.loads(blueprint_paths[name].read_text(encoding="utf-8")), "blueprint"),
		):
			fields = {
				field["fieldname"]
				for field in metadata.get("fields", [])
				if field.get("fieldname") and field.get("fieldtype") not in {"Section Break", "Column Break"}
			}
			if metadata.get("name") != name or metadata.get("module") != "HRP Workflow Authorization":
				violations.append(f"{name} {source} has the wrong owner")
			if fields != expected_fields[name]:
				violations.append(f"{name} {source} fields must match the approval-matrix contract")
		blueprint = json.loads(blueprint_paths[name].read_text(encoding="utf-8"))
		runtime = json.loads(runtime_paths[name].read_text(encoding="utf-8"))
		blueprint.pop("custom", None)
		blueprint.pop("x_hrp", None)
		if runtime != blueprint:
			violations.append(f"{name} runtime and blueprint metadata must match")
		if "?" in blueprint_paths[name].read_text(encoding="utf-8"):
			violations.append(f"{name} blueprint contains replacement question marks")

	common_text = (root / APP_NAME / "common" / "approval_matrix.py").read_text(encoding="utf-8")
	for token in (
		f"APPROVAL_MATRIX_SCHEMA_VERSION = {APPROVAL_MATRIX_SCHEMA_VERSION}",
		"build_approval_matrix_definition",
		"build_approval_evaluation",
		"build_approval_decision",
		"decision_digest",
		"parallel approvers must share step policy",
	):
		if token not in common_text:
			violations.append(f"approval matrix public contract is missing: {token}")
	for value in (*APPROVER_TYPES, *APPROVAL_MODES):
		if f'"{value}"' not in common_text:
			violations.append(f"approval matrix option is missing: {value}")
	for forbidden in ("eval(", "exec(", "frappe.safe_eval", "server_script", "frappe.db.sql("):
		if forbidden in common_text:
			violations.append(f"approval matrix contract must not execute user code: {forbidden}")

	service_text = (module_root / "services" / "approval_matrix.py").read_text(encoding="utf-8")
	for token in (
		"class UpsertApprovalMatrixService",
		"class EvaluateApprovalMatrixService",
		"HRPApprovalMatrix.lock_revision",
		"has_scoped_permission",
		"frappe.has_permission",
		"CONFIGURATION_INVALID",
		"approval_matrix_evaluated",
	):
		if token not in service_text:
			violations.append(f"approval matrix service is missing: {token}")
	if "frappe.db.commit(" in service_text:
		violations.append("approval matrix services must not commit transactions")
	for forbidden in ("eval(", "exec(", "frappe.safe_eval", "server_script"):
		if forbidden in service_text:
			violations.append(f"approval matrix service must not execute user code: {forbidden}")

	hooks_text = (root / APP_NAME / "hooks.py").read_text(encoding="utf-8")
	permissions_text = (module_root / "permissions.py").read_text(encoding="utf-8")
	if '"HRP Approval Matrix"' not in hooks_text:
		violations.append('approval matrix permission contract is missing: "HRP Approval Matrix"')
	for token in ("approval_matrix_query", "can_read_approval_matrix"):
		if token not in hooks_text or token not in permissions_text:
			violations.append(f"approval matrix permission contract is missing: {token}")

	api_text = (root / APP_NAME / "api" / "v1" / "core" / "approval.py").read_text(encoding="utf-8")
	for method_name in ("def upsert", "def evaluate", "def get"):
		if method_name not in api_text:
			violations.append(f"approval matrix API is missing method: {method_name}")
	if api_text.count("require_authenticated_user()") != 3:
		violations.append("approval matrix APIs must authenticate before parsing or service access")

	setup_text = (root / APP_NAME / "setup" / "approval_matrix.py").read_text(encoding="utf-8")
	install_text = (root / APP_NAME / "setup" / "install.py").read_text(encoding="utf-8")
	for token in (
		"idx_hrp_approval_matrix_resolution",
		"idx_hrp_approval_matrix_organization",
		"idx_hrp_approval_matrix_step",
	):
		if token not in setup_text:
			violations.append(f"approval matrix migration is missing: {token}")
	if install_text.count("ensure_approval_matrix_governance()") != 2:
		violations.append("approval matrix migration must run after install and migrate")

	workspace_text = (module_root / "workspace" / "hrp_authorization" / "hrp_authorization.json").read_text(
		encoding="utf-8"
	)
	if "HRP Approval Matrix" not in workspace_text or "审批矩阵" not in workspace_text:
		violations.append("authorization workspace must expose the approval matrix")

	with (root / "design" / "field_catalog.csv").open(encoding="utf-8-sig", newline="") as handle:
		field_rows = [row for row in csv.DictReader(handle) if row.get("doctype") in expected_fields]
	for name, fields in expected_fields.items():
		actual = {row["fieldname"] for row in field_rows if row["doctype"] == name}
		if actual != fields:
			violations.append(f"{name} field catalog must match the runtime contract")
	if any("?" in row.get("label_cn", "") or "?" in row.get("description", "") for row in field_rows):
		violations.append("approval matrix field catalog contains replacement question marks")

	endpoints = {
		"/api/method/medops.api.v1.core.approval.evaluate": (
			"CORE-002",
			"Read-only deterministic",
			"Read-only",
		),
		"/api/method/medops.api.v1.core.approval.upsert": (
			"CORE-028",
			"Required for write",
			"Single DB transaction",
		),
		"/api/method/medops.api.v1.core.approval.get": ("CORE-029", "Read-only deterministic", "Read-only"),
	}
	with (root / "api" / "api_catalog.csv").open(encoding="utf-8-sig", newline="") as handle:
		catalog_rows = {row["path"]: row for row in csv.DictReader(handle)}
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	for endpoint, (code, idempotency, boundary) in endpoints.items():
		row = catalog_rows.get(endpoint)
		if not row or row.get("api_code") != code or row.get("idempotency") != idempotency:
			violations.append(f"{code} API catalog contract is invalid")
		operation_name = "get" if code == "CORE-029" else "post"
		operation = openapi.get("paths", {}).get(endpoint, {}).get(operation_name, {})
		if operation.get("x-transaction-boundary") != boundary:
			violations.append(f"{code} OpenAPI transaction boundary is invalid")
		has_key = any(
			parameter.get("$ref") == "#/components/parameters/IdempotencyKey"
			for parameter in operation.get("parameters", [])
		)
		if has_key != (code == "CORE-028"):
			violations.append(f"{code} OpenAPI idempotency header contract is invalid")

	pure_tests = (root / "tests" / "test_approval_matrix.py").read_text(encoding="utf-8")
	integration_tests = (module_root / "tests" / "test_approval_matrix.py").read_text(encoding="utf-8")
	for token in (
		"test_dimensions_are_declarative_bounded_and_canonical",
		"test_parallel_approvers_share_policy_and_cannot_repeat",
		"test_decision_deduplicates_users_and_has_a_stable_digest",
		"test_evaluation_requires_full_context_and_money_precision",
	):
		if token not in pure_tests:
			violations.append(f"approval matrix pure tests are missing: {token}")
	for token in (
		"test_metadata_indexes_permissions_and_workspace_are_idempotent",
		"test_create_update_replay_and_stale_revision",
		"test_evaluate_resolves_real_document_and_is_deterministic",
		"test_evaluate_rejects_tampered_context_and_ambiguous_policy",
		"test_configuration_rejects_missing_dimensions_and_invalid_validity",
		"test_user_requires_role_scope_and_document_permission",
		"test_http_upsert_requires_idempotency_and_evaluate_is_read_only",
		"test_http_guest_is_rejected_before_contract_parsing",
	):
		if token not in integration_tests:
			violations.append(f"approval matrix integration tests are missing: {token}")
	return violations


def validate_delegation_contract(root: Path) -> list[str]:
	module_root = root / APP_NAME / "hrp_workflow_authorization"
	runtime_path = module_root / "doctype" / "hrp_delegation" / "hrp_delegation.json"
	blueprint_path = root / "doctype_blueprints" / "hrp_workflow_authorization" / "hrp_delegation.json"
	required_paths = (
		root / APP_NAME / "common" / "delegation.py",
		module_root / "services" / "delegation.py",
		module_root / "permissions.py",
		root / APP_NAME / "api" / "v1" / "core" / "delegation.py",
		root / APP_NAME / "setup" / "delegation.py",
		module_root
		/ "report"
		/ "hrp_delegation_and_expiry_register"
		/ "hrp_delegation_and_expiry_register.py",
		root / "architecture" / "delegation.md",
		root / "architecture" / "adr" / "ADR-0020-revision-locked-approval-delegation.md",
		root / "tests" / "test_delegation.py",
		module_root / "tests" / "test_delegation.py",
		runtime_path,
		blueprint_path,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing delegation contract file: {path}" for path in missing]

	violations: list[str] = []
	expected_fields = {
		"matrix",
		"matrix_revision",
		"matrix_digest",
		"target_doctype",
		"company",
		"hospital",
		"organization_unit",
		"include_descendants",
		"from_user",
		"to_user",
		"step_sequence",
		"valid_from",
		"valid_to",
		"status",
		"reason",
		"policy_digest",
		"revoked_at",
		"revoked_by",
		"revocation_reason",
		"expired_at",
	}
	metadata_sources = (
		(json.loads(runtime_path.read_text(encoding="utf-8")), "runtime"),
		(json.loads(blueprint_path.read_text(encoding="utf-8")), "blueprint"),
	)
	for metadata, source in metadata_sources:
		fields = {
			field["fieldname"]
			for field in metadata.get("fields", [])
			if field.get("fieldname") and field.get("fieldtype") not in {"Section Break", "Column Break"}
		}
		if (
			metadata.get("name") != DELEGATION_DOCTYPE
			or metadata.get("module") != "HRP Workflow Authorization"
		):
			violations.append(f"delegation {source} has the wrong owner")
		if fields != expected_fields:
			violations.append(f"delegation {source} fields must match the controlled contract")
		status = next(
			(field for field in metadata.get("fields", []) if field.get("fieldname") == "status"),
			{},
		)
		if tuple(str(status.get("options", "")).splitlines()) != DELEGATION_STATUSES:
			violations.append(f"delegation {source} statuses must remain closed")
		if metadata.get("is_submittable", 0) or metadata.get("allow_rename", 0):
			violations.append(f"delegation {source} must not be submittable or renameable")
	runtime = metadata_sources[0][0]
	blueprint = dict(metadata_sources[1][0])
	blueprint.pop("custom", None)
	blueprint.pop("x_hrp", None)
	if runtime != blueprint:
		violations.append("delegation runtime and blueprint metadata must match")
	if "?" in blueprint_path.read_text(encoding="utf-8"):
		violations.append("delegation blueprint contains replacement question marks")

	common_text = (root / APP_NAME / "common" / "delegation.py").read_text(encoding="utf-8")
	for token in (
		f"DELEGATION_SCHEMA_VERSION = {DELEGATION_SCHEMA_VERSION}",
		f"MAX_DELEGATION_DAYS = {MAX_DELEGATION_DAYS}",
		"build_delegation_create",
		"build_delegation_definition",
		"apply_delegation_grants",
		"from_user and to_user must be different",
		"multiple active delegations match one approval responsibility",
	):
		if token not in common_text:
			violations.append(f"delegation public contract is missing: {token}")
	for forbidden in ("eval(", "exec(", "frappe.safe_eval", "server_script", "frappe.db.sql("):
		if forbidden in common_text:
			violations.append(f"delegation contract must not execute user code: {forbidden}")

	service_text = (module_root / "services" / "delegation.py").read_text(encoding="utf-8")
	for token in (
		"class CreateDelegationService",
		"class RevokeDelegationService",
		"_owns_matrix_step",
		"_has_scope",
		"_assert_no_conflict",
		"_lock_matrix",
		"matrix_revision",
		"matrix_digest",
		"apply_active_delegations",
		"sync_delegation_statuses",
		"delegation_created",
		"delegation_revoked",
	):
		if token not in service_text:
			violations.append(f"delegation service is missing: {token}")
	if "frappe.db.commit(" in service_text:
		violations.append("delegation services must not commit transactions")
	for forbidden in ("eval(", "exec(", "frappe.safe_eval", "server_script"):
		if forbidden in service_text:
			violations.append(f"delegation service must not execute user code: {forbidden}")

	controller_text = (module_root / "doctype" / "hrp_delegation" / "hrp_delegation.py").read_text(
		encoding="utf-8"
	)
	for token in ("_require_service_write", "_validate_immutable_policy", "def on_trash"):
		if token not in controller_text:
			violations.append(f"delegation direct-write guard is missing: {token}")

	hooks_text = (root / APP_NAME / "hooks.py").read_text(encoding="utf-8")
	permissions_text = (module_root / "permissions.py").read_text(encoding="utf-8")
	for token in ("delegation_query", "can_read_delegation"):
		if token not in hooks_text or token not in permissions_text:
			violations.append(f"delegation permission contract is missing: {token}")
	if "sync_delegation_statuses" not in hooks_text:
		violations.append("delegation daily lifecycle task is missing")

	api_text = (root / APP_NAME / "api" / "v1" / "core" / "delegation.py").read_text(encoding="utf-8")
	for method_name in ("def create", "def revoke", "def get"):
		if method_name not in api_text:
			violations.append(f"delegation API is missing method: {method_name}")
	if api_text.count("require_authenticated_user()") != 3:
		violations.append("delegation APIs must authenticate before parsing or service access")

	setup_text = (root / APP_NAME / "setup" / "delegation.py").read_text(encoding="utf-8")
	install_text = (root / APP_NAME / "setup" / "install.py").read_text(encoding="utf-8")
	for token in ("idx_hrp_delegation_resolution", "idx_hrp_delegation_principals"):
		if token not in setup_text:
			violations.append(f"delegation migration is missing: {token}")
	if install_text.count("ensure_delegation_governance()") != 2:
		violations.append("delegation migration must run after install and migrate")

	workspace_text = (module_root / "workspace" / "hrp_authorization" / "hrp_authorization.json").read_text(
		encoding="utf-8"
	)
	for token in ("HRP Delegation", "审批委托", "HRP Delegation and Expiry Register"):
		if token not in workspace_text:
			violations.append(f"authorization workspace is missing delegation entry: {token}")

	with (root / "design" / "field_catalog.csv").open(encoding="utf-8-sig", newline="") as handle:
		field_rows = [row for row in csv.DictReader(handle) if row.get("doctype") == DELEGATION_DOCTYPE]
	if {row["fieldname"] for row in field_rows} != expected_fields:
		violations.append("delegation field catalog must match the runtime contract")
	if any("?" in row.get("label_cn", "") or "?" in row.get("description", "") for row in field_rows):
		violations.append("delegation field catalog contains replacement question marks")

	endpoints = {
		"/api/method/medops.api.v1.core.delegation.create": ("CORE-008", "post", True),
		"/api/method/medops.api.v1.core.delegation.revoke": ("CORE-030", "post", True),
		"/api/method/medops.api.v1.core.delegation.get": ("CORE-031", "get", False),
	}
	with (root / "api" / "api_catalog.csv").open(encoding="utf-8-sig", newline="") as handle:
		catalog_rows = {row["path"]: row for row in csv.DictReader(handle)}
	openapi = yaml.safe_load((root / "api" / "openapi.yaml").read_text(encoding="utf-8"))
	for endpoint, (code, operation_name, writes) in endpoints.items():
		row = catalog_rows.get(endpoint)
		expected_idempotency = "Required for write" if writes else "Read-only owner scoped"
		if not row or row.get("api_code") != code or row.get("idempotency") != expected_idempotency:
			violations.append(f"{code} API catalog contract is invalid")
		operation = openapi.get("paths", {}).get(endpoint, {}).get(operation_name, {})
		expected_boundary = "Single DB transaction" if writes else "Read-only"
		if operation.get("x-transaction-boundary") != expected_boundary:
			violations.append(f"{code} OpenAPI transaction boundary is invalid")
		has_key = any(
			parameter.get("$ref") == "#/components/parameters/IdempotencyKey"
			for parameter in operation.get("parameters", [])
		)
		if has_key != writes:
			violations.append(f"{code} OpenAPI idempotency header contract is invalid")

	pure_tests = (root / "tests" / "test_delegation.py").read_text(encoding="utf-8")
	integration_tests = (module_root / "tests" / "test_delegation.py").read_text(encoding="utf-8")
	for token in (
		"test_create_rejects_self_invalid_range_and_excessive_duration",
		"test_definition_digest_is_deterministic_and_locks_matrix_revision",
		"test_grant_replaces_only_matching_step_and_adds_audit_evidence",
		"test_ambiguous_grants_fail_closed",
	):
		if token not in pure_tests:
			violations.append(f"delegation pure tests are missing: {token}")
	for token in (
		"test_metadata_indexes_permissions_workspace_and_direct_write_guard",
		"test_create_replay_overlap_get_and_permission_filter",
		"test_evaluation_applies_then_revocation_restores_original_approver",
		"test_matrix_revision_change_invalidates_existing_delegation",
		"test_status_job_activates_and_expires_without_touching_revoked_records",
		"test_http_create_get_revoke_require_idempotency_for_writes",
		"test_http_guest_is_rejected_before_contract_parsing",
	):
		if token not in integration_tests:
			violations.append(f"delegation integration tests are missing: {token}")
	return violations


def validate_environment_profiles(root: Path) -> list[str]:
	profile_path = root / APP_NAME / "config" / "environment_profiles.json"
	manager_path = root / "scripts" / "environment_manager.py"
	integration_path = root / "scripts" / "ci_integration.sh"
	api_catalog_path = root / "api" / "api_catalog.csv"
	openapi_path = root / "api" / "openapi.yaml"
	required_paths = (
		profile_path,
		manager_path,
		integration_path,
		api_catalog_path,
		openapi_path,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.is_file()]
	if missing:
		return [f"missing environment delivery file: {path}" for path in missing]

	violations: list[str] = []
	try:
		registry = load_environment_registry(profile_path)
	except EnvironmentProfileError as exc:
		return [f"invalid environment profiles: {exc}"]
	if tuple(profile.name for profile in registry.profiles) != ("development", "test", "demo"):
		violations.append("environment profiles must be development, test and demo")

	manager_text = manager_path.read_text(encoding="utf-8")
	for token in (
		"bootstrap_latest_develop.sh",
		"version_lock.py",
		"list-apps",
		"migrate",
		"environment-audit.jsonl",
		"DB_ROOT_PASSWORD",
		"ADMIN_PASSWORD",
		"--allow-target-override",
	):
		if token not in manager_text:
			violations.append(f"environment manager is missing: {token}")

	integration_text = integration_path.read_text(encoding="utf-8")
	for token in (
		"environment_manager.py",
		"configure test",
		"verify test",
		'"changed": false',
	):
		if token not in integration_text:
			violations.append(f"CI does not validate environment profiles: {token}")

	environment_api = "/api/method/medops.api.v1.environment.get_environment_status"
	if environment_api not in api_catalog_path.read_text(encoding="utf-8"):
		violations.append("API catalog is missing the environment status endpoint")
	if environment_api not in openapi_path.read_text(encoding="utf-8"):
		violations.append("OpenAPI is missing the environment status endpoint")
	return violations


def validate_fixture_governance(root: Path) -> list[str]:
	policy_path = root / APP_NAME / "config" / "fixture_policy.json"
	modules_path = root / APP_NAME / "modules.txt"
	fixture_directory = root / APP_NAME / "fixtures"
	hooks_path = root / APP_NAME / "hooks.py"
	manager_path = root / "scripts" / "fixture_manager.py"
	integration_path = root / "scripts" / "ci_integration.sh"
	documentation_path = root / "architecture" / "fixtures.md"
	api_catalog_path = root / "api" / "api_catalog.csv"
	openapi_path = root / "api" / "openapi.yaml"
	required_paths = (
		policy_path,
		modules_path,
		fixture_directory,
		hooks_path,
		manager_path,
		integration_path,
		documentation_path,
		api_catalog_path,
		openapi_path,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.exists()]
	if missing:
		return [f"missing fixture governance file: {path}" for path in missing]

	violations: list[str] = []
	try:
		policy = load_fixture_policy(policy_path, modules_path=modules_path)
		report = inspect_fixture_repository(policy, fixture_directory)
	except FixturePolicyError as exc:
		return [f"invalid fixture governance: {exc}"]
	if tuple(rule.doctype for rule in policy.rules) != (
		"Custom Field",
		"Property Setter",
		"Custom DocPerm",
	):
		violations.append("fixture allowlist must contain only the approved configuration DocTypes")
	if report.files != 3:
		violations.append("fixture repository must contain all three ordered files")

	hooks_text = hooks_path.read_text(encoding="utf-8")
	for token in (
		"get_frappe_fixture_hooks",
		"fixture_auto_order = True",
		"fixtures = get_frappe_fixture_hooks()",
	):
		if token not in hooks_text:
			violations.append(f"hooks.py is missing fixture governance token: {token}")

	manager_text = manager_path.read_text(encoding="utf-8")
	for token in (
		"export-fixtures",
		"assert_fixture_export_allowed",
		"fixture-export-audit.jsonl",
		"--yes",
		"Repeated fixture export is not idempotent",
	):
		if token not in manager_text:
			violations.append(f"fixture manager is missing: {token}")

	integration_text = integration_path.read_text(encoding="utf-8")
	for token in (
		"fixture_manager.py",
		"COD-007",
		"fixture-export-audit.jsonl",
	):
		if token not in integration_text:
			violations.append(f"CI does not validate fixture governance: {token}")

	fixture_api = "/api/method/medops.api.v1.fixtures.get_fixture_governance_status"
	if fixture_api not in api_catalog_path.read_text(encoding="utf-8"):
		violations.append("API catalog is missing the fixture governance endpoint")
	if fixture_api not in openapi_path.read_text(encoding="utf-8"):
		violations.append("OpenAPI is missing the fixture governance endpoint")
	return violations


def validate_change_governance(root: Path) -> list[str]:
	policy_path = root / APP_NAME / "config" / "change_governance.json"
	manager_path = root / "scripts" / "change_manager.py"
	documentation_path = root / "architecture" / "change_governance.md"
	template_path = root / "architecture" / "ADR_TEMPLATE.md"
	adr_directory = root / "architecture" / "adr"
	change_directory = root / "changes"
	task_path = root / "backlog" / "COD-008.md"
	pr_template_path = root / ".github" / "pull_request_template.md"
	workflow_path = root / ".github" / "workflows" / "ci.yml"
	catalog_paths = (
		root / "api" / "api_catalog.csv",
		root / "api" / "api_catalog.yaml",
		root / "api" / "openapi.yaml",
	)
	required_paths = (
		policy_path,
		manager_path,
		documentation_path,
		template_path,
		adr_directory,
		change_directory,
		task_path,
		pr_template_path,
		workflow_path,
		*catalog_paths,
	)
	missing = [str(path.relative_to(root)) for path in required_paths if not path.exists()]
	if missing:
		return [f"missing change governance file: {path}" for path in missing]

	try:
		report = inspect_change_governance(root)
	except ChangeGovernanceError as exc:
		return [f"invalid change governance: {exc}"]
	violations: list[str] = []
	if report.policy.baseline_task_id != "COD-008":
		violations.append("change governance baseline task must be COD-008")
	if len(report.decisions) < 2:
		violations.append("change governance must include the baseline architecture decisions")
	if len(report.changes) < 8:
		violations.append("change governance must backfill completed COD-001 through COD-007")
	if "COD-008" not in report.change_by_task:
		violations.append("change governance must include COD-008")

	manager_text = manager_path.read_text(encoding="utf-8")
	for token in (
		"inspect_change_governance",
		"assess_changed_paths",
		"validate_adr_transition",
		"merge-base",
		"medops-change-governance-audit.jsonl",
		"production_write_enabled",
		"http_write_enabled",
	):
		if token not in manager_text:
			violations.append(f"change manager is missing: {token}")

	workflow_text = workflow_path.read_text(encoding="utf-8")
	for token in (
		"CHANGE_BASE",
		"python scripts/change_manager.py check",
		"python scripts/change_manager.py validate",
	):
		if token not in workflow_text:
			violations.append(f"CI does not enforce change governance: {token}")

	pr_template_text = pr_template_path.read_text(encoding="utf-8")
	for token in ("ADR 编号", "变更记录", "破坏性变更", "change_manager.py"):
		if token not in pr_template_text:
			violations.append(f"PR template is missing change governance evidence: {token}")

	governance_api = "/api/method/medops.api.v1.change_governance.get_change_governance_status"
	for catalog_path in catalog_paths:
		if governance_api not in catalog_path.read_text(encoding="utf-8"):
			violations.append(f"{catalog_path.relative_to(root)} is missing the change governance endpoint")
	return violations


def collect_violations(root: Path) -> list[str]:
	root = root.resolve()
	violations: list[str] = []
	apps = discover_custom_apps(root)
	if set(apps) != {APP_NAME}:
		violations.append(f"repository must contain exactly {APP_NAME}; found {sorted(apps)}")
	apps_root = root / "apps"
	if any((apps_root / upstream).exists() for upstream in UPSTREAM_APPS):
		violations.append("upstream Frappe/ERPNext/HRMS source must not be vendored or modified")
	if apps_root.is_dir() and any(
		path.name.startswith(f"{APP_NAME}_") for path in apps_root.iterdir() if path.is_dir()
	):
		violations.append("business domains must be modules, not additional medops_* apps")

	app_root = root
	package_root = root / APP_NAME
	if not (package_root / "__init__.py").is_file():
		violations.append("missing medops Python package")
	elif apps.get(APP_NAME) != app_root:
		violations.append("medops pyproject must be at repository root for Press")
	if not (package_root / "hooks.py").is_file():
		violations.append("missing Press-discoverable medops/hooks.py")

	violations.extend(find_legacy_prefix_references(root))
	violations.extend(validate_branch_policy(root))
	violations.extend(validate_push_guard(root))
	violations.extend(validate_quality_tooling(root))
	violations.extend(validate_ci_pipeline(root))
	violations.extend(validate_version_baseline(root))
	violations.extend(validate_catalog_ownership(root))
	violations.extend(validate_module_structure(root))
	violations.extend(validate_module_boundaries(root))
	violations.extend(validate_protected_ledgers(root))
	violations.extend(validate_environment_profiles(root))
	violations.extend(validate_fixture_governance(root))
	violations.extend(validate_change_governance(root))
	violations.extend(validate_error_contract(root))
	violations.extend(validate_audit_context_contract(root))
	violations.extend(validate_domain_service_contract(root))
	violations.extend(validate_immutable_ledger_contract(root))
	violations.extend(validate_transactional_message_contract(root))
	violations.extend(validate_test_data_factory_contract(root))
	violations.extend(validate_performance_baseline_contract(root))
	violations.extend(validate_software_supply_chain_contract(root))
	violations.extend(validate_system_settings_contract(root))
	violations.extend(validate_organization_hierarchy_contract(root))
	violations.extend(validate_organization_mapping_contract(root))
	violations.extend(validate_master_data_governance_contract(root))
	violations.extend(validate_external_code_mapping_contract(root))
	violations.extend(validate_data_quality_contract(root))
	violations.extend(validate_numbering_contract(root))
	violations.extend(validate_access_scope_contract(root))
	violations.extend(validate_approval_matrix_contract(root))
	violations.extend(validate_delegation_contract(root))
	return violations


def main() -> int:
	root = Path(__file__).resolve().parents[1]
	violations = collect_violations(root)
	if violations:
		print("REPOSITORY CONTRACT FAILED", file=sys.stderr)
		for violation in violations:
			print(f"- {violation}", file=sys.stderr)
		return 1
	print(
		json.dumps(
			{
				"status": "ok",
				"custom_app": APP_NAME,
				"modules": 36,
				"protected_branch": "main",
				"pull_requests": "required",
			},
			ensure_ascii=False,
		)
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
