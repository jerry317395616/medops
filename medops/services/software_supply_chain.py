from __future__ import annotations

from typing import Any

from medops.common.software_supply_chain import (
	SUPPLY_CHAIN_ROLES,
	SoftwareSupplyChainContractError,
	SoftwareSupplyChainPolicy,
	load_software_supply_chain_policy,
)
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.errors import raise_ione_error, require_roles

SOFTWARE_SUPPLY_CHAIN_SERVICE_ROLES = frozenset(SUPPLY_CHAIN_ROLES)


def _load_policy() -> SoftwareSupplyChainPolicy:
	try:
		return load_software_supply_chain_policy()
	except SoftwareSupplyChainContractError as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)


def get_software_supply_chain_contract_status() -> dict[str, Any]:
	with service_audit_scope():
		require_roles(SOFTWARE_SUPPLY_CHAIN_SERVICE_ROLES)
		policy = _load_policy()
		result = policy.as_public_dict()
		result["scan_available_from_site"] = False
		result["artifact_storage"] = "ci_or_release_artifact"
		emit_audit_event(
			"software_supply_chain_contract_read",
			logger_name="medops.software_supply_chain",
			policy_sha256=policy.sha256,
			tool_count=len(policy.tools),
			exception_count=len(policy.exceptions),
			external_execution_only=True,
		)
		return result


__all__ = ["get_software_supply_chain_contract_status"]
