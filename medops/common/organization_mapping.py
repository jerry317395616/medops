from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from medops.common.organization import (
	OrganizationContractError,
	normalize_boolean,
	normalize_nonnegative_integer,
	normalize_optional_text,
	normalize_reference,
)

ORGANIZATION_MAPPING_SCHEMA_VERSION = 1
ORGANIZATION_MAPPING_DOCTYPE = "HRP Organization Mapping"


class OrganizationMappingContractError(ValueError):
	"""Raised when an organization mapping request is malformed."""


def _translate_contract_error(exc: OrganizationContractError) -> OrganizationMappingContractError:
	return OrganizationMappingContractError(str(exc))


def normalize_optional_reference(value: object, *, label: str) -> str | None:
	if value in (None, ""):
		return None
	if not isinstance(value, str) or value != value.strip():
		raise OrganizationMappingContractError(f"{label} is invalid")
	try:
		return normalize_reference(value, label=label)
	except OrganizationContractError as exc:
		raise _translate_contract_error(exc) from exc


@dataclass(frozen=True, slots=True)
class OrganizationMappingUpsert:
	organization_version: str
	organization_unit: str
	department: str | None
	cost_center: str | None
	enabled: bool
	expected_revision: int
	remarks: str | None

	def as_request_payload(self) -> dict[str, object]:
		remarks_digest = (
			sha256(self.remarks.encode("utf-8")).hexdigest() if self.remarks is not None else None
		)
		return {
			"organization_version": self.organization_version,
			"organization_unit": self.organization_unit,
			"department": self.department,
			"cost_center": self.cost_center,
			"enabled": self.enabled,
			"expected_revision": self.expected_revision,
			"remarks_digest": remarks_digest,
		}


def build_organization_mapping_upsert(
	*,
	organization_version: object,
	organization_unit: object,
	department: object = None,
	cost_center: object = None,
	enabled: object = True,
	expected_revision: object = 0,
	remarks: object = None,
) -> OrganizationMappingUpsert:
	try:
		normalized_version = normalize_reference(
			organization_version,
			label="organization_version",
		)
		normalized_unit = normalize_reference(
			organization_unit,
			label="organization_unit",
		)
		normalized_enabled = normalize_boolean(enabled, label="enabled")
		normalized_revision = normalize_nonnegative_integer(
			expected_revision,
			label="expected_revision",
		)
		normalized_remarks = normalize_optional_text(remarks, label="remarks")
	except OrganizationContractError as exc:
		raise _translate_contract_error(exc) from exc

	normalized_department = normalize_optional_reference(department, label="department")
	normalized_cost_center = normalize_optional_reference(cost_center, label="cost_center")
	if normalized_department is None and normalized_cost_center is None:
		raise OrganizationMappingContractError(
			"department or cost_center is required",
		)
	return OrganizationMappingUpsert(
		organization_version=normalized_version,
		organization_unit=normalized_unit,
		department=normalized_department,
		cost_center=normalized_cost_center,
		enabled=normalized_enabled,
		expected_revision=normalized_revision,
		remarks=normalized_remarks,
	)


@dataclass(frozen=True, slots=True)
class OrganizationMappingResolve:
	organization_unit: str | None
	hospital: str | None
	unit_code: str | None
	effective_on: str | None

	def as_request_payload(self) -> dict[str, object]:
		return {
			"organization_unit": self.organization_unit,
			"hospital": self.hospital,
			"unit_code": self.unit_code,
			"effective_on": self.effective_on,
		}


def build_organization_mapping_resolve(
	*,
	organization_unit: object = None,
	hospital: object = None,
	unit_code: object = None,
	effective_on: object = None,
) -> OrganizationMappingResolve:
	from medops.common.organization import normalize_code, normalize_optional_date

	normalized_unit = normalize_optional_reference(
		organization_unit,
		label="organization_unit",
	)
	normalized_hospital = normalize_optional_reference(hospital, label="hospital")
	try:
		normalized_code = (
			normalize_code(unit_code, label="unit_code") if unit_code not in (None, "") else None
		)
		normalized_date = normalize_optional_date(effective_on, label="effective_on")
	except OrganizationContractError as exc:
		raise _translate_contract_error(exc) from exc

	direct = normalized_unit is not None
	dated = normalized_hospital is not None or normalized_code is not None
	if direct == dated:
		raise OrganizationMappingContractError(
			"provide organization_unit or hospital with unit_code",
		)
	if dated and (normalized_hospital is None or normalized_code is None):
		raise OrganizationMappingContractError(
			"hospital and unit_code are required together",
		)
	if direct and normalized_date is not None:
		raise OrganizationMappingContractError(
			"effective_on is only valid with hospital and unit_code",
		)
	return OrganizationMappingResolve(
		organization_unit=normalized_unit,
		hospital=normalized_hospital,
		unit_code=normalized_code,
		effective_on=normalized_date,
	)


__all__ = [
	"ORGANIZATION_MAPPING_DOCTYPE",
	"ORGANIZATION_MAPPING_SCHEMA_VERSION",
	"OrganizationMappingContractError",
	"OrganizationMappingResolve",
	"OrganizationMappingUpsert",
	"build_organization_mapping_resolve",
	"build_organization_mapping_upsert",
	"normalize_optional_reference",
]
