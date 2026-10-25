from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import ClassVar, Literal, NoReturn

import frappe
from frappe.model.document import Document
from frappe.utils import cint

from medops.common.domain_service import DomainServiceDefinition
from medops.common.immutable_ledger import (
	BASE_LEDGER_FIELDS,
	ImmutableLedgerContractError,
	ImmutableLedgerDefinition,
	ImmutableLedgerPublicContract,
	assert_reversal_matches,
	build_reversal_values,
	get_immutable_ledger_public_contract,
	normalize_dimensions_json,
	normalize_ledger_name,
	normalize_ledger_values,
	normalize_optional_source_hash,
)
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_roles

LEDGER_CONTRACT_ROLES = frozenset({"System Manager", "HRP System Manager"})
NUMERIC_FIELDTYPES = frozenset({"Currency", "Float", "Int", "Percent"})
FORBIDDEN_LEDGER_PERMISSIONS = (
	"create",
	"write",
	"delete",
	"submit",
	"cancel",
	"amend",
	"import",
)
LedgerOperation = Literal["append", "reverse"]


@dataclass(frozen=True, slots=True)
class AppendLedgerEntry:
	values: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ReverseLedgerEntry:
	entry_name: str
	overrides: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _LedgerWriteContext:
	definition: ImmutableLedgerDefinition
	operation: LedgerOperation
	original: Mapping[str, object] | None = None


_LEDGER_WRITE_CONTEXT: ContextVar[_LedgerWriteContext | None] = ContextVar(
	"medops_immutable_ledger_write_context",
	default=None,
)


def _fieldnames(doctype: str) -> frozenset[str]:
	return frozenset(
		field.fieldname
		for field in frappe.get_meta(doctype).fields
		if field.fieldname and field.fieldtype not in {"Column Break", "Section Break", "Tab Break"}
	)


def _raise_configuration_error(message: str) -> NoReturn:
	raise_ione_error(
		"CONFIGURATION_INVALID",
		cause=ImmutableLedgerContractError(message),
	)


def validate_immutable_ledger_doctype(definition: ImmutableLedgerDefinition) -> frozenset[str]:
	"""Fail closed when a concrete business ledger does not satisfy the shared schema."""
	if not frappe.db.exists("DocType", definition.doctype):
		_raise_configuration_error("immutable ledger DocType does not exist")
	meta = frappe.get_meta(definition.doctype)
	for property_name in ("issingle", "istable", "is_submittable", "allow_rename", "track_changes"):
		if cint(meta.get(property_name)):
			_raise_configuration_error(f"immutable ledger property must be disabled: {property_name}")

	fields = {field.fieldname: field for field in meta.fields if field.fieldname}
	for contract in BASE_LEDGER_FIELDS:
		field = fields.get(contract.fieldname)
		if field is None or field.fieldtype != contract.fieldtype:
			_raise_configuration_error(f"immutable ledger field contract is invalid: {contract.fieldname}")
		if contract.required and not cint(field.reqd):
			_raise_configuration_error(f"immutable ledger field must be required: {contract.fieldname}")
	if fields["voucher_no"].options != "voucher_type":
		_raise_configuration_error("voucher_no must be linked through voucher_type")
	if fields["reference_name"].options != "reference_type":
		_raise_configuration_error("reference_name must be linked through reference_type")
	if fields["reversal_of"].options != definition.doctype:
		_raise_configuration_error("reversal_of must link to the same ledger DocType")
	if fields["dimensions_json"].options != "JSON":
		_raise_configuration_error("dimensions_json must use JSON code mode")

	all_fieldnames = frozenset(fields)
	for fieldname in definition.transformed_fields:
		field = fields.get(fieldname)
		if field is None or field.fieldtype not in NUMERIC_FIELDTYPES:
			_raise_configuration_error(f"immutable ledger reversal field must be numeric: {fieldname}")
	missing_overrides = definition.reversal_override_fields - all_fieldnames
	if missing_overrides:
		_raise_configuration_error(
			"immutable ledger is missing reversal override fields: " + ", ".join(sorted(missing_overrides))
		)

	read_roles = {
		permission.role for permission in meta.permissions if permission.role and cint(permission.read)
	}
	if not definition.required_roles.issubset(read_roles):
		_raise_configuration_error("immutable ledger roles must have read permission")
	for permission in meta.permissions:
		if any(cint(permission.get(permission_name)) for permission_name in FORBIDDEN_LEDGER_PERMISSIONS):
			_raise_configuration_error("immutable ledger permissions must be read-only")
	return all_fieldnames


def _definition_for(document: Document) -> ImmutableLedgerDefinition:
	definition = getattr(type(document), "ledger_definition", None)
	if not isinstance(definition, ImmutableLedgerDefinition):
		_raise_configuration_error("immutable ledger controller has no valid definition")
	return definition


def _require_write_context(
	document: Document,
	*,
	expected_operation: LedgerOperation | None = None,
) -> _LedgerWriteContext:
	definition = _definition_for(document)
	context = _LEDGER_WRITE_CONTEXT.get()
	if (
		context is None
		or context.definition != definition
		or (expected_operation is not None and context.operation != expected_operation)
	):
		raise_ione_error("OPERATION_NOT_ALLOWED")
	return context


@contextmanager
def _ledger_write_scope(
	definition: ImmutableLedgerDefinition,
	operation: LedgerOperation,
	*,
	original: Mapping[str, object] | None = None,
):
	token = _LEDGER_WRITE_CONTEXT.set(
		_LedgerWriteContext(
			definition=definition,
			operation=operation,
			original=original,
		)
	)
	try:
		yield
	finally:
		_LEDGER_WRITE_CONTEXT.reset(token)


class ImmutableLedgerDocument(Document):
	"""Append-only controller base used by concrete HRP business ledger DocTypes."""

	ledger_definition: ClassVar[ImmutableLedgerDefinition]

	def before_validate(self) -> None:
		self.is_reversal = cint(self.get("is_reversal"))
		self.dimensions_json = normalize_dimensions_json(self.get("dimensions_json"))
		self.source_hash = normalize_optional_source_hash(self.get("source_hash"))

	def before_insert(self) -> None:
		context = _require_write_context(self)
		validate_immutable_ledger_doctype(context.definition)

	def validate(self) -> None:
		if not self.is_new():
			raise_ione_error("OPERATION_NOT_ALLOWED")
		context = _require_write_context(self)
		is_reversal = bool(cint(self.get("is_reversal")))
		if context.operation == "append":
			if is_reversal or self.get("reversal_of"):
				raise_ione_error("INVALID_STATE_TRANSITION")
			return
		if not is_reversal or not self.get("reversal_of") or context.original is None:
			raise_ione_error("INVALID_STATE_TRANSITION")
		try:
			assert_reversal_matches(
				context.original,
				self.as_dict(),
				definition=context.definition,
			)
		except ImmutableLedgerContractError as exc:
			raise_ione_error("INVALID_STATE_TRANSITION", cause=exc)

	def db_insert(self, *args, **kwargs) -> None:
		_require_write_context(self)
		super().db_insert(*args, **kwargs)

	def db_update(self, *args, **kwargs) -> None:
		del args, kwargs
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_set(self, *args, **kwargs):
		del args, kwargs
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_cancel(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_update_after_submit(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def on_trash(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_rename(self, old: str, new: str, merge: bool = False):
		del old, new, merge
		raise_ione_error("OPERATION_NOT_ALLOWED")


class AppendImmutableLedgerService(DomainService[AppendLedgerEntry]):
	"""Base service for one concrete ledger's append command."""

	ledger_definition: ClassVar[ImmutableLedgerDefinition]
	definition: ClassVar[DomainServiceDefinition]

	def _validate_configuration(self) -> frozenset[str]:
		if self.definition.kind != "command":
			_raise_configuration_error("immutable ledger service must be a command")
		if self.definition.required_roles != self.ledger_definition.required_roles:
			_raise_configuration_error("ledger and service roles differ")
		return validate_immutable_ledger_doctype(self.ledger_definition)

	def validate(self, command: AppendLedgerEntry) -> None:
		fieldnames = self._validate_configuration()
		values = normalize_ledger_values(command.values, allowed_fields=fieldnames)
		if "is_reversal" in values or "reversal_of" in values:
			raise ImmutableLedgerContractError("append command cannot set reversal fields")

	def request_payload(self, command: AppendLedgerEntry) -> dict[str, object]:
		fieldnames = _fieldnames(self.ledger_definition.doctype)
		return {
			"doctype": self.ledger_definition.doctype,
			"values": normalize_ledger_values(command.values, allowed_fields=fieldnames),
		}

	def perform(self, command: AppendLedgerEntry) -> dict[str, object]:
		fieldnames = _fieldnames(self.ledger_definition.doctype)
		values = normalize_ledger_values(command.values, allowed_fields=fieldnames)
		values["is_reversal"] = 0
		values["reversal_of"] = None
		with _ledger_write_scope(self.ledger_definition, "append"):
			document = frappe.get_doc(
				{
					"doctype": self.ledger_definition.doctype,
					**values,
				}
			).insert(ignore_permissions=True)
		emit_audit_event(
			"immutable_ledger_appended",
			logger_name="medops.immutable_ledger",
			ledger_doctype=self.ledger_definition.doctype,
		)
		return {
			"doctype": self.ledger_definition.doctype,
			"entry": document.name,
			"is_reversal": False,
		}


class ReverseImmutableLedgerService(DomainService[ReverseLedgerEntry]):
	"""Base service for one concrete ledger's locked, equal-and-opposite reversal."""

	ledger_definition: ClassVar[ImmutableLedgerDefinition]
	definition: ClassVar[DomainServiceDefinition]

	def _validate_configuration(self) -> frozenset[str]:
		if self.definition.kind != "command":
			_raise_configuration_error("immutable ledger service must be a command")
		if self.definition.required_roles != self.ledger_definition.required_roles:
			_raise_configuration_error("ledger and service roles differ")
		return validate_immutable_ledger_doctype(self.ledger_definition)

	def validate(self, command: ReverseLedgerEntry) -> None:
		self._validate_configuration()
		normalize_ledger_name(command.entry_name)
		normalize_ledger_values(
			command.overrides,
			allowed_fields=self.ledger_definition.reversal_override_fields,
		)

	def request_payload(self, command: ReverseLedgerEntry) -> dict[str, object]:
		return {
			"doctype": self.ledger_definition.doctype,
			"entry_name": normalize_ledger_name(command.entry_name),
			"overrides": normalize_ledger_values(
				command.overrides,
				allowed_fields=self.ledger_definition.reversal_override_fields,
			),
		}

	def perform(self, command: ReverseLedgerEntry) -> dict[str, object]:
		entry_name = normalize_ledger_name(command.entry_name)
		try:
			locked_name = frappe.db.get_value(
				self.ledger_definition.doctype,
				entry_name,
				"name",
				for_update=True,
				wait=False,
			)
		except (frappe.QueryTimeoutError, frappe.QueryDeadlockError) as exc:
			raise_ione_error("CONFLICT", cause=exc)
		if not locked_name:
			raise_ione_error("RESOURCE_NOT_FOUND")

		original = frappe.get_doc(self.ledger_definition.doctype, entry_name)
		if cint(original.get("is_reversal")):
			raise_ione_error("INVALID_STATE_TRANSITION")
		if frappe.db.get_value(
			self.ledger_definition.doctype,
			{"reversal_of": entry_name},
			"name",
		):
			raise_ione_error("INVALID_STATE_TRANSITION")

		fieldnames = self._validate_configuration()
		try:
			values = build_reversal_values(
				original.as_dict(),
				definition=self.ledger_definition,
				fieldnames=fieldnames,
				overrides=command.overrides,
			)
		except ImmutableLedgerContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		with _ledger_write_scope(
			self.ledger_definition,
			"reverse",
			original=original.as_dict(),
		):
			reversal = frappe.get_doc(
				{
					"doctype": self.ledger_definition.doctype,
					**values,
				}
			).insert(ignore_permissions=True)
		emit_audit_event(
			"immutable_ledger_reversed",
			logger_name="medops.immutable_ledger",
			ledger_doctype=self.ledger_definition.doctype,
		)
		return {
			"doctype": self.ledger_definition.doctype,
			"entry": reversal.name,
			"is_reversal": True,
			"reversal_of": entry_name,
		}


def get_immutable_ledger_contract_status() -> ImmutableLedgerPublicContract:
	with service_audit_scope():
		require_roles(LEDGER_CONTRACT_ROLES)
		result = get_immutable_ledger_public_contract()
		emit_audit_event(
			"immutable_ledger_contract_read",
			logger_name="medops.immutable_ledger",
			contract_version=result["schema_version"],
			base_field_count=len(result["base_fields"]),
		)
		return result


__all__ = [
	"AppendImmutableLedgerService",
	"AppendLedgerEntry",
	"ImmutableLedgerDocument",
	"ReverseImmutableLedgerService",
	"ReverseLedgerEntry",
	"get_immutable_ledger_contract_status",
	"validate_immutable_ledger_doctype",
]
