from __future__ import annotations

import frappe

from medops.common.constants import CORE_ROLES
from medops.services.errors import raise_ione_error
from medops.setup.access_scope import ensure_access_scope_governance
from medops.setup.approval_matrix import ensure_approval_matrix_governance
from medops.setup.budget_planning import ensure_budget_planning
from medops.setup.budget_submission import ensure_budget_submission
from medops.setup.business_rules import ensure_business_rule_governance
from medops.setup.core_reports import ensure_core_reports
from medops.setup.delegation import ensure_delegation_governance
from medops.setup.master_data import ensure_master_data_governance
from medops.setup.master_data_access import ensure_master_data_access_contracts
from medops.setup.modules import sync_module_defs, sync_module_settings
from medops.setup.numbering import ensure_numbering_governance
from medops.setup.organization import ensure_organization_hierarchy
from medops.setup.segregation import ensure_segregation_governance
from medops.setup.settings import ensure_system_settings
from medops.setup.sla import ensure_sla_governance
from medops.setup.versions import validate_runtime_versions
from medops.setup.workspaces import sync_owned_workspaces


def before_install() -> None:
	if "ione" + "_hrp" in frappe.get_installed_apps():
		raise_ione_error(
			"CONFIGURATION_INVALID",
			cause=RuntimeError("MedOps requires a fresh site; legacy HRP coexistence is unsupported"),
		)


def _ensure_roles() -> None:
	for role_name in CORE_ROLES:
		if frappe.db.exists("Role", role_name):
			continue
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": role_name,
				"desk_access": 1,
				"is_custom": 0,
			}
		).insert(ignore_permissions=True)


def after_install() -> None:
	validate_runtime_versions()
	sync_module_defs()
	_ensure_roles()
	sync_module_settings()
	ensure_organization_hierarchy()
	ensure_master_data_governance()
	ensure_master_data_access_contracts()
	ensure_numbering_governance()
	ensure_access_scope_governance()
	ensure_approval_matrix_governance()
	ensure_delegation_governance()
	ensure_segregation_governance()
	ensure_sla_governance()
	ensure_business_rule_governance()
	ensure_system_settings()
	ensure_core_reports()
	ensure_budget_planning()
	ensure_budget_submission()
	sync_owned_workspaces()


def after_migrate() -> None:
	validate_runtime_versions()
	sync_module_defs()
	_ensure_roles()
	sync_module_settings()
	ensure_organization_hierarchy()
	ensure_master_data_governance()
	ensure_master_data_access_contracts()
	ensure_numbering_governance()
	ensure_access_scope_governance()
	ensure_approval_matrix_governance()
	ensure_delegation_governance()
	ensure_segregation_governance()
	ensure_sla_governance()
	ensure_business_rule_governance()
	ensure_system_settings()
	ensure_core_reports()
	ensure_budget_planning()
	ensure_budget_submission()
	sync_owned_workspaces()


def before_uninstall() -> None:
	# Frappe displays a destructive uninstall confirmation and removes Module Def-owned records.
	# This hook exists so future releases can add domain-specific archival checks.
	return None
