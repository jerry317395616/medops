"""Acquire a budget guard only after locking its material-request source."""

from __future__ import annotations

from dataclasses import dataclass

import frappe

from medops.common.budget_control import ordered_budget_keys
from medops.common.domain_service import normalize_sha256
from medops.common.organization import normalize_reference
from medops.hrp_budget.services.control_guards import (
	MAX_BOUND_MATERIAL_SOURCES,
	acquire_multi_source_bound_budget_guards,
	acquire_source_bound_budget_guards,
	assert_material_guard_binding,
	assert_material_guard_bindings,
	invalidate_budget_guards,
)
from medops.hrp_budget.services.material_request_source import (
	SOURCE_FIELDS,
	MaterialRequestSource,
	locked_material_request_source,
)
from medops.hrp_budget.services.planning import native_document
from medops.services.errors import raise_ione_error


@dataclass(frozen=True, slots=True)
class MaterialSourceReference:
	request: str
	line: str
	organization_unit: str
	expected_digest: str

	def __post_init__(self):
		for field in ("request", "line", "organization_unit"):
			value = getattr(self, field)
			if normalize_reference(value, label=field) != value:
				raise ValueError("canonical material source reference required")
		if normalize_sha256(self.expected_digest, label="source_digest") != self.expected_digest:
			raise ValueError("canonical source digest required")


@dataclass(frozen=True, slots=True)
class MaterialSourceIdentity:
	"""Untrusted historical identity; current facts are obtained under native locks."""

	request: str
	line: str
	organization_unit: str

	def __post_init__(self):
		for field in ("request", "line", "organization_unit"):
			value = getattr(self, field)
			if normalize_reference(value, label=field) != value:
				raise ValueError("canonical material source identity required")


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


def acquire_material_budget_guards_for_sources(
	references: object, keys: object
) -> tuple[MaterialRequestSource, ...]:
	"""Lock all named purchase parents first, then verify sources and guards.

	The caller must discover the complete source set independently and compare it
	again under the guard. A changed set requires a whole-transaction retry; this
	helper must start before other source or master locks in an owned transaction;
	it cannot detect arbitrary prior locks. It does not prove source completeness,
	approval, or available funds. It grants no write authority and the caller must
	fully roll back on any failure. A MaterialSourceIdentity binds current facts
	without checking a historical digest; callers must independently authenticate
	and compare every historical snapshot before using any resulting observation.
	"""
	try:
		if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is not None:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if (
			not isinstance(references, (list, tuple))
			or not 1 <= len(references) <= MAX_BOUND_MATERIAL_SOURCES
		):
			raise ValueError("bounded material source references required")
		if any(
			type(reference) not in {MaterialSourceReference, MaterialSourceIdentity}
			for reference in references
		):
			raise ValueError("canonical material source references required")
		ordered_refs = tuple(
			sorted(references, key=lambda item: (item.request, item.line, item.organization_unit))
		)
		identities = {(item.request, item.line, item.organization_unit) for item in ordered_refs}
		if len(identities) != len(ordered_refs):
			raise ValueError("duplicate material source reference")
		ordered_keys = ordered_budget_keys(keys)
		requests = sorted({item.request for item in ordered_refs})
		# Check every parent without locks first, then lock all canonical parents
		# in a shared order before taking any dependent source/master locks.
		for lock in (False, True):
			for name in requests:
				doc = native_document("Material Request", name, SOURCE_FIELDS, lock=lock)
				if doc.name != name:
					raise_ione_error("CONFLICT")
		sources = []
		for reference in ordered_refs:
			source = locked_material_request_source(
				reference.request, reference.line, reference.organization_unit
			)
			if (source.request, source.line, source.organization_unit) != (
				reference.request,
				reference.line,
				reference.organization_unit,
			) or (
				type(reference) is MaterialSourceReference and source.digest() != reference.expected_digest
			):
				raise_ione_error("CONFLICT")
			sources.append(source)

		def same_scope(source, key):
			return (source.company, source.hospital, source.organization_unit, source.project) == (
				key.company,
				key.hospital,
				key.organization_unit,
				key.project,
			)

		if any(not any(same_scope(source, key) for key in ordered_keys) for source in sources) or any(
			not any(same_scope(source, key) for source in sources) for key in ordered_keys
		):
			raise ValueError("material sources and budget guard scopes differ")
		bindings = tuple(sorted(_binding(source) for source in sources))
		acquire_multi_source_bound_budget_guards(ordered_keys, source_bindings=bindings)
		for source in sources:
			current = locked_material_request_source(source.request, source.line, source.organization_unit)
			if current != source:
				raise_ione_error("CONFLICT")
		assert_material_guard_bindings(ordered_keys, bindings)
		return tuple(sources)
	except BaseException:
		invalidate_budget_guards()
		raise
