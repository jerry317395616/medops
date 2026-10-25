from __future__ import annotations

from typing import Any

from medops.common.performance_baseline import (
	PERFORMANCE_BASELINE_ROLES,
	PerformanceBaselineContractError,
	PerformanceBaselineRegistry,
	load_performance_baseline_registry,
)
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.environment import get_environment_status
from medops.services.errors import raise_ione_error, require_roles

PERFORMANCE_BASELINE_SERVICE_ROLES = frozenset(PERFORMANCE_BASELINE_ROLES)


def _load_registry() -> PerformanceBaselineRegistry:
	try:
		return load_performance_baseline_registry()
	except PerformanceBaselineContractError as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)


def _is_load_test_available(
	status: dict[str, object],
	registry: PerformanceBaselineRegistry,
) -> bool:
	policy = registry.policy
	return bool(
		status.get("managed")
		and status.get("name") in policy.allowed_profiles
		and status.get("allow_tests")
		and status.get("synthetic_data_only")
		and not status.get("public_access")
		and not status.get("external_integrations_enabled")
	)


def get_performance_baseline_contract_status() -> dict[str, Any]:
	with service_audit_scope():
		require_roles(PERFORMANCE_BASELINE_SERVICE_ROLES)
		registry = _load_registry()
		status = get_environment_status()
		result = registry.as_public_dict()
		result["environment"] = {
			"managed": bool(status.get("managed")),
			"name": status.get("name"),
		}
		result["load_test_available"] = _is_load_test_available(status, registry)
		emit_audit_event(
			"performance_baseline_contract_read",
			logger_name="medops.performance_baseline",
			registry_sha256=registry.sha256,
			scenario_count=len(registry.scenarios),
			load_test_available=result["load_test_available"],
		)
		return result


__all__ = [
	"get_performance_baseline_contract_status",
]
