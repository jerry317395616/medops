from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from medops.common.organization import (
	OrganizationContractError,
	normalize_boolean,
	normalize_code,
	normalize_nonnegative_integer,
	normalize_optional_date,
	normalize_optional_text,
	normalize_positive_integer,
	normalize_reference,
	normalize_required_date,
	normalize_required_text,
	validate_date_range,
)

EXTERNAL_CODE_MAPPING_SCHEMA_VERSION = 1
EXTERNAL_CODE_MAPPING_DOCTYPE = "HRP External Code Mapping"
GLOBAL_SCOPE_KEY = "*"
MAX_EXTERNAL_CODE_LENGTH = 140
MAX_EXTERNAL_LABEL_LENGTH = 140


class ExternalCodeMappingContractError(ValueError):
	"""Raised when an external-code mapping command is malformed."""


def _translate_contract_error(
	exc: OrganizationContractError,
) -> ExternalCodeMappingContractError:
	return ExternalCodeMappingContractError(str(exc))


def normalize_optional_reference(value: object, *, label: str) -> str | None:
	if value in (None, ""):
		return None
	try:
		return normalize_reference(value, label=label)
	except OrganizationContractError as exc:
		raise _translate_contract_error(exc) from exc


def normalize_external_code(value: object) -> str:
	if not isinstance(value, str) or value != value.strip():
		raise ExternalCodeMappingContractError("external_code is invalid")
	try:
		return normalize_required_text(
			value,
			label="external_code",
			maximum=MAX_EXTERNAL_CODE_LENGTH,
		)
	except OrganizationContractError as exc:
		raise _translate_contract_error(exc) from exc


def scope_key_for(organization_unit: str | None) -> str:
	return organization_unit or GLOBAL_SCOPE_KEY


def _mapping_key(direction: str, parts: tuple[str, ...]) -> str:
	serialized = "\0".join(("medops-external-code-mapping-v1", direction, *parts))
	return sha256(serialized.encode("utf-8")).hexdigest()


def source_key_for(
	*,
	master_data_domain: str,
	company: str,
	hospital: str,
	scope_key: str,
	external_system: str,
	external_code: str,
) -> str:
	return _mapping_key(
		"source",
		(
			master_data_domain,
			company,
			hospital,
			scope_key,
			external_system,
			external_code,
		),
	)


def target_key_for(
	*,
	master_data_domain: str,
	company: str,
	hospital: str,
	scope_key: str,
	external_system: str,
	internal_name: str,
) -> str:
	return _mapping_key(
		"target",
		(
			master_data_domain,
			company,
			hospital,
			scope_key,
			external_system,
			internal_name,
		),
	)


@dataclass(frozen=True, slots=True)
class ExternalCodeMappingUpsert:
	mapping_name: str | None
	master_data_domain: str
	company: str
	hospital: str
	organization_unit: str | None
	external_system: str
	external_code: str
	external_label: str | None
	internal_name: str
	enabled: bool
	valid_from: str
	valid_to: str | None
	expected_revision: int
	remarks: str | None

	@property
	def scope_key(self) -> str:
		return scope_key_for(self.organization_unit)

	@property
	def source_key(self) -> str:
		return source_key_for(
			master_data_domain=self.master_data_domain,
			company=self.company,
			hospital=self.hospital,
			scope_key=self.scope_key,
			external_system=self.external_system,
			external_code=self.external_code,
		)

	@property
	def target_key(self) -> str:
		return target_key_for(
			master_data_domain=self.master_data_domain,
			company=self.company,
			hospital=self.hospital,
			scope_key=self.scope_key,
			external_system=self.external_system,
			internal_name=self.internal_name,
		)

	def as_request_payload(self) -> dict[str, object]:
		return {
			"mapping_name": self.mapping_name,
			"master_data_domain": self.master_data_domain,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit,
			"external_system": self.external_system,
			"external_code": self.external_code,
			"external_label": self.external_label,
			"internal_name": self.internal_name,
			"enabled": self.enabled,
			"valid_from": self.valid_from,
			"valid_to": self.valid_to,
			"expected_revision": self.expected_revision,
			"remarks_digest": (
				sha256(self.remarks.encode("utf-8")).hexdigest() if self.remarks is not None else None
			),
		}


def build_external_code_mapping_upsert(
	*,
	master_data_domain: object,
	company: object,
	hospital: object,
	external_system: object,
	external_code: object,
	internal_name: object,
	valid_from: object,
	organization_unit: object = None,
	external_label: object = None,
	enabled: object = True,
	valid_to: object = None,
	expected_revision: object = 0,
	mapping_name: object = None,
	remarks: object = None,
) -> ExternalCodeMappingUpsert:
	try:
		normalized_domain = normalize_code(master_data_domain, label="master_data_domain")
		normalized_company = normalize_reference(company, label="company")
		normalized_hospital = normalize_code(hospital, label="hospital")
		normalized_system = normalize_code(external_system, label="external_system")
		normalized_internal_name = normalize_reference(internal_name, label="internal_name")
		normalized_valid_from = normalize_required_date(valid_from, label="valid_from")
		normalized_valid_to = normalize_optional_date(valid_to, label="valid_to")
		normalized_enabled = normalize_boolean(enabled, label="enabled")
		normalized_revision = normalize_nonnegative_integer(
			expected_revision,
			label="expected_revision",
		)
		normalized_external_label = normalize_optional_text(
			external_label,
			label="external_label",
			maximum=MAX_EXTERNAL_LABEL_LENGTH,
		)
		normalized_remarks = normalize_optional_text(remarks, label="remarks")
	except OrganizationContractError as exc:
		raise _translate_contract_error(exc) from exc
	try:
		validate_date_range(normalized_valid_from, normalized_valid_to)
	except OrganizationContractError as exc:
		raise _translate_contract_error(exc) from exc

	normalized_mapping_name = normalize_optional_reference(mapping_name, label="mapping_name")
	normalized_organization_unit = normalize_optional_reference(
		organization_unit,
		label="organization_unit",
	)
	if normalized_mapping_name is None and normalized_revision != 0:
		raise ExternalCodeMappingContractError("new mappings require expected_revision 0")
	if normalized_mapping_name is not None:
		try:
			normalized_revision = normalize_positive_integer(
				normalized_revision,
				label="expected_revision",
			)
		except OrganizationContractError as exc:
			raise _translate_contract_error(exc) from exc
	return ExternalCodeMappingUpsert(
		mapping_name=normalized_mapping_name,
		master_data_domain=normalized_domain,
		company=normalized_company,
		hospital=normalized_hospital,
		organization_unit=normalized_organization_unit,
		external_system=normalized_system,
		external_code=normalize_external_code(external_code),
		external_label=normalized_external_label,
		internal_name=normalized_internal_name,
		enabled=normalized_enabled,
		valid_from=normalized_valid_from,
		valid_to=normalized_valid_to,
		expected_revision=normalized_revision,
		remarks=normalized_remarks,
	)


@dataclass(frozen=True, slots=True)
class ExternalCodeMappingResolve:
	master_data_domain: str
	company: str
	hospital: str
	organization_unit: str | None
	external_system: str
	external_code: str
	effective_on: str

	@property
	def scope_key(self) -> str:
		return scope_key_for(self.organization_unit)

	@property
	def source_key(self) -> str:
		return source_key_for(
			master_data_domain=self.master_data_domain,
			company=self.company,
			hospital=self.hospital,
			scope_key=self.scope_key,
			external_system=self.external_system,
			external_code=self.external_code,
		)

	def as_request_payload(self) -> dict[str, object]:
		return {
			"master_data_domain": self.master_data_domain,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit,
			"external_system": self.external_system,
			"external_code": self.external_code,
			"effective_on": self.effective_on,
		}


def build_external_code_mapping_resolve(
	*,
	master_data_domain: object,
	company: object,
	hospital: object,
	external_system: object,
	external_code: object,
	effective_on: object,
	organization_unit: object = None,
) -> ExternalCodeMappingResolve:
	try:
		return ExternalCodeMappingResolve(
			master_data_domain=normalize_code(
				master_data_domain,
				label="master_data_domain",
			),
			company=normalize_reference(company, label="company"),
			hospital=normalize_code(hospital, label="hospital"),
			organization_unit=normalize_optional_reference(
				organization_unit,
				label="organization_unit",
			),
			external_system=normalize_code(external_system, label="external_system"),
			external_code=normalize_external_code(external_code),
			effective_on=normalize_required_date(effective_on, label="effective_on"),
		)
	except OrganizationContractError as exc:
		raise _translate_contract_error(exc) from exc


@dataclass(frozen=True, slots=True)
class InternalCodeMappingResolve:
	master_data_domain: str
	company: str
	hospital: str
	organization_unit: str | None
	external_system: str
	internal_name: str
	effective_on: str

	@property
	def scope_key(self) -> str:
		return scope_key_for(self.organization_unit)

	@property
	def target_key(self) -> str:
		return target_key_for(
			master_data_domain=self.master_data_domain,
			company=self.company,
			hospital=self.hospital,
			scope_key=self.scope_key,
			external_system=self.external_system,
			internal_name=self.internal_name,
		)

	def as_request_payload(self) -> dict[str, object]:
		return {
			"master_data_domain": self.master_data_domain,
			"company": self.company,
			"hospital": self.hospital,
			"organization_unit": self.organization_unit,
			"external_system": self.external_system,
			"internal_name": self.internal_name,
			"effective_on": self.effective_on,
		}


def build_internal_code_mapping_resolve(
	*,
	master_data_domain: object,
	company: object,
	hospital: object,
	external_system: object,
	internal_name: object,
	effective_on: object,
	organization_unit: object = None,
) -> InternalCodeMappingResolve:
	try:
		return InternalCodeMappingResolve(
			master_data_domain=normalize_code(
				master_data_domain,
				label="master_data_domain",
			),
			company=normalize_reference(company, label="company"),
			hospital=normalize_code(hospital, label="hospital"),
			organization_unit=normalize_optional_reference(
				organization_unit,
				label="organization_unit",
			),
			external_system=normalize_code(external_system, label="external_system"),
			internal_name=normalize_reference(internal_name, label="internal_name"),
			effective_on=normalize_required_date(effective_on, label="effective_on"),
		)
	except OrganizationContractError as exc:
		raise _translate_contract_error(exc) from exc


__all__ = [
	"EXTERNAL_CODE_MAPPING_DOCTYPE",
	"EXTERNAL_CODE_MAPPING_SCHEMA_VERSION",
	"GLOBAL_SCOPE_KEY",
	"ExternalCodeMappingContractError",
	"ExternalCodeMappingResolve",
	"ExternalCodeMappingUpsert",
	"InternalCodeMappingResolve",
	"build_external_code_mapping_resolve",
	"build_external_code_mapping_upsert",
	"build_internal_code_mapping_resolve",
	"normalize_external_code",
	"scope_key_for",
	"source_key_for",
	"target_key_for",
]
