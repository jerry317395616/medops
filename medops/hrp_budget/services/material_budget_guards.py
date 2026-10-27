"""Acquire a budget guard only after locking its material-request source."""

from __future__ import annotations

import frappe

from medops.common.budget_control import ordered_budget_keys
from medops.hrp_budget.services.control_guards import (
	acquire_source_bound_budget_guards,
	assert_material_guard_binding,
	invalidate_budget_guards,
)
from medops.hrp_budget.services.material_request_source import (
	MaterialRequestSource,
	locked_material_request_source,
)
from medops.services.errors import raise_ione_error


def _binding(source: MaterialRequestSource) -> tuple[str, str, str, str]:
	if type(source) is not MaterialRequestSource:
		raise ValueError("material source snapshot required")
	return (source.request, source.line, source.organization_unit, source.digest())


def acquire_material_budget_guards(source: MaterialRequestSource, keys: object):
	"""Lock and recheck source before obtaining a source-bound budget guard.

	The caller must fully roll back on failure. This enforces the cooperating
	purchase path's order; it cannot control other writers, direct SQL, implicit
	commits or the complete lock ordering needed for financial authorization.
	"""
	try:
		binding = _binding(source)
		if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is not None:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		ordered = ordered_budget_keys(keys)
		for key in ordered:
			if (key.company, key.hospital, key.organization_unit, key.project) != (
				source.company,
				source.hospital,
				source.organization_unit,
				source.project,
			):
				raise ValueError("budget guard differs from material source scope")
		current = locked_material_request_source(source.request, source.line, source.organization_unit)
		if current != source or current.digest() != binding[3]:
			raise_ione_error("CONFLICT")
		return acquire_source_bound_budget_guards(ordered, source_binding=binding)
	except BaseException:
		invalidate_budget_guards()
		raise


def assert_material_budget_guards(source: MaterialRequestSource, keys: object) -> None:
	assert_material_guard_binding(keys, _binding(source))
