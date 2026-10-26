from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Generic, Literal, NoReturn, TypeVar, cast

import frappe
from frappe.model.document import Document
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from medops.common.domain_service import DomainServiceDefinition, normalize_sha256
from medops.common.transactional_message import (
	BASE_MESSAGE_FIELDS,
	IMMUTABLE_MESSAGE_FIELDS,
	MessageBoxDefinition,
	MessageSnapshot,
	TransactionalMessageContractError,
	TransactionalMessagePublicContract,
	get_transactional_message_public_contract,
	message_record_name,
	normalize_endpoint,
	normalize_error_details,
	normalize_event_id,
	normalize_event_type,
	normalize_message_snapshot,
	normalize_optional_result,
	normalize_payload,
	normalize_payload_schema_version,
	normalize_processing_token,
	processing_token_hash,
	processing_token_matches,
)
from medops.services.audit_context import (
	emit_audit_event,
	ensure_audit_context,
	service_audit_scope,
)
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_roles

if TYPE_CHECKING:
	from frappe.types import DF

MESSAGE_CONTRACT_ROLES = frozenset({"System Manager", "HRP System Manager"})
FORBIDDEN_MESSAGE_PERMISSIONS = (
	"create",
	"write",
	"delete",
	"submit",
	"cancel",
	"amend",
	"import",
)
MessageOperation = Literal["publish", "claim", "deliver", "fail", "begin", "complete"]
CommandT = TypeVar("CommandT")

_TRANSITIONS = {
	"outbox": {
		"publish": frozenset({(None, "Pending")}),
		"claim": frozenset(
			{
				("Pending", "Processing"),
				("Failed", "Processing"),
				("Processing", "Processing"),
			}
		),
		"deliver": frozenset({("Processing", "Delivered")}),
		"fail": frozenset(
			{
				("Processing", "Failed"),
				("Processing", "Dead Letter"),
			}
		),
	},
	"inbox": {
		"begin": frozenset(
			{
				(None, "Processing"),
				("Failed", "Processing"),
				("Processing", "Processing"),
			}
		),
		"complete": frozenset({("Processing", "Processed")}),
		"fail": frozenset(
			{
				("Processing", "Failed"),
				("Processing", "Ignored"),
			}
		),
	},
}
_OPERATION_MUTABLE_FIELDS = {
	"publish": frozenset(
		{
			*IMMUTABLE_MESSAGE_FIELDS,
			"status",
			"attempt_count",
			"available_at",
		}
	),
	"begin": frozenset(
		{
			*IMMUTABLE_MESSAGE_FIELDS,
			"status",
			"attempt_count",
			"available_at",
			"claim_owner",
			"processing_token_hash",
			"lease_expires_at",
			"completed_at",
			"last_error_code",
			"last_error_message",
		}
	),
	"claim": frozenset(
		{
			"status",
			"attempt_count",
			"available_at",
			"claim_owner",
			"processing_token_hash",
			"lease_expires_at",
			"completed_at",
			"last_error_code",
			"last_error_message",
		}
	),
	"deliver": frozenset(
		{
			"status",
			"claim_owner",
			"processing_token_hash",
			"lease_expires_at",
			"completed_at",
			"last_error_code",
			"last_error_message",
		}
	),
	"complete": frozenset(
		{
			"status",
			"claim_owner",
			"processing_token_hash",
			"lease_expires_at",
			"completed_at",
			"result_json",
			"result_hash",
			"last_error_code",
			"last_error_message",
		}
	),
	"fail": frozenset(
		{
			"status",
			"available_at",
			"claim_owner",
			"processing_token_hash",
			"lease_expires_at",
			"completed_at",
			"last_error_code",
			"last_error_message",
		}
	),
}
_MESSAGE_DATETIME_FIELDS = frozenset({"occurred_at", "available_at", "lease_expires_at", "completed_at"})


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
	event_id: str
	event_type: str
	source: str
	destination: str
	occurred_at: str
	payload: Mapping[str, object]
	payload_schema_version: int = 1
	causation_id: str | None = None
	reference_doctype: str | None = None
	reference_name: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimOutboxMessage:
	message_name: str
	worker: str
	lease_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class CompleteMessage:
	message_name: str
	processing_token: str
	result: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class FailMessage:
	message_name: str
	processing_token: str
	error_code: str
	error_message: str
	retry_after_seconds: int = 60


@dataclass(frozen=True, slots=True)
class _MessageWriteContext:
	definition: MessageBoxDefinition
	operation: MessageOperation


_MESSAGE_WRITE_CONTEXT: ContextVar[_MessageWriteContext | None] = ContextVar(
	"medops_transactional_message_write_context",
	default=None,
)


def _raise_configuration_error(message: str) -> NoReturn:
	raise_ione_error(
		"CONFIGURATION_INVALID",
		cause=TransactionalMessageContractError(message),
	)


def _definition_for(document: Document) -> MessageBoxDefinition:
	definition = getattr(type(document), "message_box_definition", None)
	if not isinstance(definition, MessageBoxDefinition):
		_raise_configuration_error("transactional message controller has no valid definition")
	return definition


def _require_write_context(
	document: Document,
	*,
	expected_operation: MessageOperation | None = None,
) -> _MessageWriteContext:
	definition = _definition_for(document)
	context = _MESSAGE_WRITE_CONTEXT.get()
	if (
		context is None
		or context.definition != definition
		or (expected_operation is not None and context.operation != expected_operation)
	):
		raise_ione_error("OPERATION_NOT_ALLOWED")
	return context


@contextmanager
def _message_write_scope(
	definition: MessageBoxDefinition,
	operation: MessageOperation,
):
	token = _MESSAGE_WRITE_CONTEXT.set(_MessageWriteContext(definition=definition, operation=operation))
	try:
		yield
	finally:
		_MESSAGE_WRITE_CONTEXT.reset(token)


def _field_map(doctype: str) -> dict[str, object]:
	return {field.fieldname: field for field in frappe.get_meta(doctype).fields if field.fieldname}


def validate_message_box_doctype(definition: MessageBoxDefinition) -> frozenset[str]:
	"""Fail closed when a concrete Outbox or Inbox does not implement the base contract."""
	if not frappe.db.exists("DocType", definition.doctype):
		_raise_configuration_error("transactional message DocType does not exist")
	meta = frappe.get_meta(definition.doctype)
	for property_name in ("issingle", "istable", "is_submittable", "allow_rename", "track_changes"):
		if cint(meta.get(property_name)):
			_raise_configuration_error(f"transactional message property must be disabled: {property_name}")

	fields = _field_map(definition.doctype)
	for contract in BASE_MESSAGE_FIELDS:
		field = fields.get(contract.fieldname)
		if field is None or getattr(field, "fieldtype", None) != contract.fieldtype:
			_raise_configuration_error(
				f"transactional message field contract is invalid: {contract.fieldname}"
			)
		# Frappe rejects hidden mandatory fields without a static default. Hidden
		# service-owned values are still required by _normalize_stored_document.
		if contract.required and not contract.hidden and not cint(getattr(field, "reqd", 0)):
			_raise_configuration_error(f"transactional message field must be required: {contract.fieldname}")
		if contract.hidden and not cint(getattr(field, "hidden", 0)):
			_raise_configuration_error(f"transactional message field must be hidden: {contract.fieldname}")
	if getattr(fields["payload_json"], "options", None) != "JSON":
		_raise_configuration_error("payload_json must use JSON code mode")
	if getattr(fields["result_json"], "options", None) != "JSON":
		_raise_configuration_error("result_json must use JSON code mode")
	if getattr(fields["reference_name"], "options", None) != "reference_doctype":
		_raise_configuration_error("reference_name must be linked through reference_doctype")
	expected_statuses = "\n".join(definition.statuses)
	if getattr(fields["status"], "options", None) != expected_statuses:
		_raise_configuration_error("transactional message statuses do not match the definition")

	read_roles = {
		permission.role for permission in meta.permissions if permission.role and cint(permission.read)
	}
	if not definition.required_roles.issubset(read_roles):
		_raise_configuration_error("transactional message roles must have read permission")
	for permission in meta.permissions:
		if any(cint(permission.get(permission_name)) for permission_name in FORBIDDEN_MESSAGE_PERMISSIONS):
			_raise_configuration_error("transactional message permissions must be read-only")
	return frozenset(fields)


def _normalize_stored_document(document: Document, definition: MessageBoxDefinition) -> None:
	try:
		snapshot = normalize_message_snapshot(
			event_id=document.get("event_id"),
			event_type=document.get("event_type"),
			source=document.get("source"),
			destination=document.get("destination"),
			occurred_at=str(document.get("occurred_at") or ""),
			payload_schema_version=document.get("payload_schema_version"),
			payload=document.get("payload_json"),
			correlation_id=document.get("correlation_id"),
			causation_id=document.get("causation_id"),
			reference_doctype=document.get("reference_doctype"),
			reference_name=document.get("reference_name"),
		)
		if document.get("payload_hash") not in (None, "", snapshot["payload_hash"]):
			raise TransactionalMessageContractError("payload_hash does not match payload_json")
		for fieldname, value in snapshot.items():
			document.set(fieldname, value)
		if document.get("status") not in definition.statuses:
			raise TransactionalMessageContractError("message status is invalid")
		attempt_count = cint(document.get("attempt_count"))
		if attempt_count < 0 or attempt_count > definition.max_attempts:
			raise TransactionalMessageContractError("attempt_count is invalid")
		document.attempt_count = attempt_count

		result = normalize_optional_result(document.get("result_json"))
		if result is None:
			if document.get("result_hash"):
				raise TransactionalMessageContractError("result_hash exists without result_json")
			document.result_json = None
			document.result_hash = None
		else:
			if document.get("result_hash") not in (None, "", result[2]):
				raise TransactionalMessageContractError("result_hash does not match result_json")
			document.result_json = result[1]
			document.result_hash = result[2]

		if document.status == "Processing":
			if (
				not document.get("claim_owner")
				or not document.get("processing_token_hash")
				or not document.get("lease_expires_at")
			):
				raise TransactionalMessageContractError("processing message has no active lease")
			normalize_sha256(
				document.processing_token_hash,
				label="processing_token_hash",
			)
		elif any(
			document.get(fieldname)
			for fieldname in ("claim_owner", "processing_token_hash", "lease_expires_at")
		):
			raise TransactionalMessageContractError("non-processing message contains an active lease")

		terminal_statuses = (
			{"Delivered", "Dead Letter"} if definition.kind == "outbox" else {"Processed", "Ignored"}
		)
		if document.status in terminal_statuses and not document.get("completed_at"):
			raise TransactionalMessageContractError("terminal message has no completion time")
		if document.status not in terminal_statuses and document.get("completed_at"):
			raise TransactionalMessageContractError("non-terminal message has a completion time")
		if document.status in {"Failed", "Dead Letter", "Ignored"}:
			normalize_error_details(
				document.get("last_error_code"),
				document.get("last_error_message"),
			)
		elif document.get("last_error_code") or document.get("last_error_message"):
			raise TransactionalMessageContractError("successful message contains error details")
	except (TransactionalMessageContractError, ValueError, TypeError) as exc:
		raise_ione_error("INVALID_REQUEST", cause=exc)


def _message_field_values_equal(fieldname: str, left: object, right: object) -> bool:
	if fieldname not in _MESSAGE_DATETIME_FIELDS:
		return left == right
	if left in (None, "") or right in (None, ""):
		return left in (None, "") and right in (None, "")
	try:
		return get_datetime(left) == get_datetime(right)
	except (TypeError, ValueError):
		return False


class TransactionalMessageDocument(Document):
	"""Controller base that allows only declared message services to mutate records."""

	message_box_definition: ClassVar[MessageBoxDefinition]

	if TYPE_CHECKING:
		attempt_count: DF.Int
		available_at: DF.Datetime
		causation_id: DF.Data | None
		claim_owner: DF.Data | None
		completed_at: DF.Datetime | None
		correlation_id: DF.Data
		destination: DF.Data
		event_id: DF.Data
		event_type: DF.Data
		last_error_code: DF.Data | None
		last_error_message: DF.SmallText | None
		lease_expires_at: DF.Datetime | None
		occurred_at: DF.Datetime
		payload_hash: DF.Data
		payload_json: DF.Code
		payload_schema_version: DF.Int
		processing_token_hash: DF.Data | None
		reference_doctype: DF.Link | None
		reference_name: DF.DynamicLink | None
		result_hash: DF.Data | None
		result_json: DF.Code | None
		source: DF.Data
		status: DF.Data

	def before_insert(self) -> None:
		_require_write_context(self)
		validate_message_box_doctype(self.message_box_definition)

	def before_validate(self) -> None:
		_normalize_stored_document(self, self.message_box_definition)

	def validate(self) -> None:
		context = _require_write_context(self)
		previous = self.get_doc_before_save()
		previous_status = previous.get("status") if previous is not None else None
		transition = (previous_status, self.get("status"))
		allowed = _TRANSITIONS[self.message_box_definition.kind].get(context.operation)
		if allowed is None or transition not in allowed:
			raise_ione_error("INVALID_STATE_TRANSITION")
		if previous is None:
			return
		for fieldname in IMMUTABLE_MESSAGE_FIELDS:
			if not _message_field_values_equal(
				fieldname,
				self.get(fieldname),
				previous.get(fieldname),
			):
				raise_ione_error("OPERATION_NOT_ALLOWED")
		changed = frozenset(
			fieldname
			for fieldname in self.as_dict()
			if not _message_field_values_equal(
				fieldname,
				self.get(fieldname),
				previous.get(fieldname),
			)
		)
		allowed_changes = _OPERATION_MUTABLE_FIELDS[context.operation]
		for fieldname in changed:
			if fieldname in {"modified", "modified_by"}:
				continue
			if fieldname not in allowed_changes:
				raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_insert(self, *args, **kwargs) -> None:
		_require_write_context(self)
		super().db_insert(*args, **kwargs)

	def db_update(self, *args, **kwargs) -> None:
		_require_write_context(self)
		super().db_update(*args, **kwargs)

	def db_set(self, *args, **kwargs):
		del args, kwargs
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def on_trash(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_rename(self, old: str, new: str, merge: bool = False):
		del old, new, merge
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_cancel(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_update_after_submit(self) -> None:
		raise_ione_error("OPERATION_NOT_ALLOWED")


def _validate_message_name(value: object) -> str:
	if not isinstance(value, str):
		raise TransactionalMessageContractError("message_name is invalid")
	prefix_length = 4 if value.startswith("out-") else 3 if value.startswith("in-") else 0
	digest = value[prefix_length:]
	if (
		prefix_length == 0
		or len(digest) != 64
		or any(character not in "0123456789abcdef" for character in digest)
	):
		raise TransactionalMessageContractError("message_name is invalid")
	return value


def _validate_lease_seconds(value: object, definition: MessageBoxDefinition) -> int:
	if value is None:
		return definition.default_lease_seconds
	if not isinstance(value, int) or isinstance(value, bool) or not 30 <= value <= 3600:
		raise TransactionalMessageContractError("lease_seconds is invalid")
	return value


def _validate_retry_seconds(value: object) -> int:
	if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 86400:
		raise TransactionalMessageContractError("retry_after_seconds is invalid")
	return value


def _command_payload(envelope: MessageEnvelope) -> dict[str, object]:
	event_id = normalize_event_id(envelope.event_id)
	event_type = normalize_event_type(envelope.event_type)
	source = normalize_endpoint(envelope.source, label="source")
	destination = normalize_endpoint(envelope.destination, label="destination")
	if (
		not isinstance(envelope.occurred_at, str)
		or not envelope.occurred_at
		or envelope.occurred_at != envelope.occurred_at.strip()
	):
		raise TransactionalMessageContractError("occurred_at is invalid")
	payload, _, _ = normalize_payload(envelope.payload)
	snapshot = normalize_message_snapshot(
		event_id=event_id,
		event_type=event_type,
		source=source,
		destination=destination,
		occurred_at=envelope.occurred_at,
		payload_schema_version=normalize_payload_schema_version(envelope.payload_schema_version),
		payload=payload,
		correlation_id="request-contract",
		causation_id=envelope.causation_id,
		reference_doctype=envelope.reference_doctype,
		reference_name=envelope.reference_name,
	)
	snapshot.pop("correlation_id")
	snapshot.pop("payload_json")
	snapshot.pop("payload_hash")
	return {
		**snapshot,
		"payload": payload,
	}


def _snapshot_for_perform(envelope: MessageEnvelope) -> MessageSnapshot:
	context = ensure_audit_context()
	return normalize_message_snapshot(
		event_id=envelope.event_id,
		event_type=envelope.event_type,
		source=envelope.source,
		destination=envelope.destination,
		occurred_at=envelope.occurred_at,
		payload_schema_version=envelope.payload_schema_version,
		payload=envelope.payload,
		correlation_id=context.correlation_id,
		causation_id=envelope.causation_id,
		reference_doctype=envelope.reference_doctype,
		reference_name=envelope.reference_name,
	)


def _lock_message(
	definition: MessageBoxDefinition,
	message_name: str,
) -> TransactionalMessageDocument:
	try:
		locked_name = frappe.db.get_value(
			definition.doctype,
			message_name,
			"name",
			for_update=True,
			wait=False,
		)
	except frappe.QueryTimeoutError as exc:
		raise_ione_error("CONFLICT", cause=exc)
	if not locked_name:
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(
		TransactionalMessageDocument,
		frappe.get_doc(definition.doctype, message_name, for_update=True),
	)


def _clear_processing_lease(document: TransactionalMessageDocument) -> None:
	document.claim_owner = None
	document.processing_token_hash = None
	document.lease_expires_at = None


def read_message_document(
	definition: MessageBoxDefinition, message_name: str, *, lock=False
) -> TransactionalMessageDocument:
	"""Internal owned adapter read; callers must enforce their business source scope.

	There is no generic HTTP message reader. Payloads and lease hashes must never
	be returned by an adapter's public projection or placed in its job arguments.
	"""
	require_roles(definition.required_roles)
	validate_message_box_doctype(definition)
	name = _validate_message_name(message_name)
	if lock:
		return _lock_message(definition, name)
	if not frappe.db.exists(definition.doctype, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	return cast(TransactionalMessageDocument, frappe.get_doc(definition.doctype, name))


def _assert_processing_token(
	document: TransactionalMessageDocument,
	processing_token: object,
) -> None:
	if document.status != "Processing":
		raise_ione_error("INVALID_STATE_TRANSITION")
	try:
		if not processing_token_matches(
			processing_token,
			document.processing_token_hash,
		):
			raise_ione_error("CONFLICT")
	except TransactionalMessageContractError as exc:
		raise_ione_error("INVALID_REQUEST", cause=exc)


class _MessageBoxService(Generic[CommandT], DomainService[CommandT]):
	message_box_definition: ClassVar[MessageBoxDefinition]
	definition: ClassVar[DomainServiceDefinition]
	expected_kind: ClassVar[Literal["outbox", "inbox"]]

	def _validate_configuration(self) -> None:
		if self.definition.kind != "command":
			_raise_configuration_error("transactional message service must be a command")
		if self.message_box_definition.kind != self.expected_kind:
			_raise_configuration_error("transactional message service has the wrong box kind")
		if self.definition.required_roles != self.message_box_definition.required_roles:
			_raise_configuration_error("message box and service roles differ")
		validate_message_box_doctype(self.message_box_definition)


class PublishOutboxService(_MessageBoxService[MessageEnvelope]):
	"""Persist an event in the caller's transaction without performing external I/O."""

	expected_kind = "outbox"

	def validate(self, command: MessageEnvelope) -> None:
		self._validate_configuration()
		_command_payload(command)

	def request_payload(self, command: MessageEnvelope) -> dict[str, object]:
		return _command_payload(command)

	def perform(self, command: MessageEnvelope) -> dict[str, object]:
		snapshot = _snapshot_for_perform(command)
		name = message_record_name("outbox", snapshot["event_id"])
		if frappe.db.exists(self.message_box_definition.doctype, name):
			raise_ione_error("CONFLICT")
		now = now_datetime()
		document = frappe.get_doc(
			{
				"doctype": self.message_box_definition.doctype,
				"name": name,
				**snapshot,
				"status": "Pending",
				"attempt_count": 0,
				"available_at": now,
			}
		)
		try:
			with _message_write_scope(self.message_box_definition, "publish"):
				document.insert(ignore_permissions=True)
		except frappe.DuplicateEntryError as exc:
			raise_ione_error("CONFLICT", cause=exc)
		emit_audit_event(
			"transactional_outbox_published",
			logger_name="medops.transactional_message",
			box_kind="outbox",
			box_doctype=self.message_box_definition.doctype,
			box_status="Pending",
		)
		return {
			"message": name,
			"status": "Pending",
		}


class ClaimOutboxService(_MessageBoxService[ClaimOutboxMessage]):
	"""Claim one pending Outbox row; transport runs only after this transaction commits."""

	expected_kind = "outbox"

	def validate(self, command: ClaimOutboxMessage) -> None:
		self._validate_configuration()
		_validate_message_name(command.message_name)
		normalize_endpoint(command.worker, label="worker")
		_validate_lease_seconds(command.lease_seconds, self.message_box_definition)

	def request_payload(self, command: ClaimOutboxMessage) -> dict[str, object]:
		return {
			"message_name": _validate_message_name(command.message_name),
			"worker": normalize_endpoint(command.worker, label="worker"),
			"lease_seconds": _validate_lease_seconds(
				command.lease_seconds,
				self.message_box_definition,
			),
		}

	def perform(self, command: ClaimOutboxMessage) -> dict[str, object]:
		document = _lock_message(
			self.message_box_definition,
			_validate_message_name(command.message_name),
		)
		now = now_datetime()
		if document.status == "Processing":
			if get_datetime(document.lease_expires_at) > now:
				raise_ione_error("CONFLICT")
		elif document.status not in {"Pending", "Failed"}:
			raise_ione_error("INVALID_STATE_TRANSITION")
		if get_datetime(document.available_at) > now:
			raise_ione_error("CONFLICT")
		if cint(document.attempt_count) >= self.message_box_definition.max_attempts:
			raise_ione_error("INVALID_STATE_TRANSITION")

		token = secrets.token_urlsafe(32)
		document.status = "Processing"
		document.attempt_count = cint(document.attempt_count) + 1
		document.claim_owner = normalize_endpoint(command.worker, label="worker")
		document.processing_token_hash = processing_token_hash(token)
		document.lease_expires_at = add_to_date(
			now,
			seconds=_validate_lease_seconds(
				command.lease_seconds,
				self.message_box_definition,
			),
		)
		document.completed_at = None
		document.last_error_code = None
		document.last_error_message = None
		with _message_write_scope(self.message_box_definition, "claim"):
			document.save(ignore_permissions=True)
		emit_audit_event(
			"transactional_outbox_claimed",
			logger_name="medops.transactional_message",
			box_kind="outbox",
			box_doctype=self.message_box_definition.doctype,
			box_status="Processing",
			attempt_count=cint(document.attempt_count),
		)
		return {
			"message": document.name,
			"status": document.status,
			"attempt_count": cint(document.attempt_count),
			"processing_token": token,
			"lease_expires_at": str(document.lease_expires_at),
		}


class CompleteOutboxService(_MessageBoxService[CompleteMessage]):
	expected_kind = "outbox"

	def validate(self, command: CompleteMessage) -> None:
		self._validate_configuration()
		_validate_message_name(command.message_name)
		normalize_processing_token(command.processing_token)
		if command.result not in (None, {}):
			raise TransactionalMessageContractError(
				"Outbox completion does not persist a transport response payload"
			)

	def request_payload(self, command: CompleteMessage) -> dict[str, object]:
		return {
			"message_name": _validate_message_name(command.message_name),
			"processing_token_hash": processing_token_hash(command.processing_token),
		}

	def perform(self, command: CompleteMessage) -> dict[str, object]:
		document = _lock_message(
			self.message_box_definition,
			_validate_message_name(command.message_name),
		)
		_assert_processing_token(document, command.processing_token)
		document.status = "Delivered"
		document.completed_at = now_datetime()
		document.last_error_code = None
		document.last_error_message = None
		_clear_processing_lease(document)
		with _message_write_scope(self.message_box_definition, "deliver"):
			document.save(ignore_permissions=True)
		emit_audit_event(
			"transactional_outbox_delivered",
			logger_name="medops.transactional_message",
			box_kind="outbox",
			box_doctype=self.message_box_definition.doctype,
			box_status="Delivered",
			attempt_count=cint(document.attempt_count),
		)
		return {
			"message": document.name,
			"status": document.status,
			"attempt_count": cint(document.attempt_count),
		}


class FailOutboxService(_MessageBoxService[FailMessage]):
	expected_kind = "outbox"

	def validate(self, command: FailMessage) -> None:
		self._validate_configuration()
		_validate_message_name(command.message_name)
		normalize_processing_token(command.processing_token)
		normalize_error_details(command.error_code, command.error_message)
		_validate_retry_seconds(command.retry_after_seconds)

	def request_payload(self, command: FailMessage) -> dict[str, object]:
		error_code, error_message = normalize_error_details(
			command.error_code,
			command.error_message,
		)
		return {
			"message_name": _validate_message_name(command.message_name),
			"processing_token_hash": processing_token_hash(command.processing_token),
			"error_code": error_code,
			"error_message": error_message,
			"retry_after_seconds": _validate_retry_seconds(command.retry_after_seconds),
		}

	def perform(self, command: FailMessage) -> dict[str, object]:
		document = _lock_message(
			self.message_box_definition,
			_validate_message_name(command.message_name),
		)
		_assert_processing_token(document, command.processing_token)
		error_code, error_message = normalize_error_details(
			command.error_code,
			command.error_message,
		)
		now = now_datetime()
		dead_letter = cint(document.attempt_count) >= self.message_box_definition.max_attempts
		document.status = "Dead Letter" if dead_letter else "Failed"
		document.available_at = add_to_date(
			now,
			seconds=_validate_retry_seconds(command.retry_after_seconds),
		)
		document.completed_at = now if dead_letter else None
		document.last_error_code = error_code
		document.last_error_message = error_message
		_clear_processing_lease(document)
		with _message_write_scope(self.message_box_definition, "fail"):
			document.save(ignore_permissions=True)
		emit_audit_event(
			"transactional_outbox_failed",
			level="warning",
			logger_name="medops.transactional_message",
			box_kind="outbox",
			box_doctype=self.message_box_definition.doctype,
			box_status=document.status,
			attempt_count=cint(document.attempt_count),
			error_code=error_code,
		)
		return {
			"message": document.name,
			"status": document.status,
			"attempt_count": cint(document.attempt_count),
		}


def _assert_existing_inbox_matches(
	document: TransactionalMessageDocument,
	snapshot: MessageSnapshot,
) -> None:
	for fieldname in IMMUTABLE_MESSAGE_FIELDS - {"correlation_id"}:
		if not _message_field_values_equal(
			fieldname,
			document.get(fieldname),
			snapshot[fieldname],
		):
			raise_ione_error("IDEMPOTENCY_CONFLICT")


def _inbox_replay_result(document: TransactionalMessageDocument) -> dict[str, object]:
	result: dict[str, object] | None = None
	if document.result_json:
		parsed = json.loads(document.result_json)
		if isinstance(parsed, dict):
			result = parsed
	return {
		"message": document.name,
		"status": document.status,
		"should_process": False,
		"duplicate": True,
		"result": result,
	}


class BeginInboxService(_MessageBoxService[MessageEnvelope]):
	"""Reserve one consumer/event pair in the same transaction as its business effects."""

	expected_kind = "inbox"

	def validate(self, command: MessageEnvelope) -> None:
		self._validate_configuration()
		_command_payload(command)

	def request_payload(self, command: MessageEnvelope) -> dict[str, object]:
		return _command_payload(command)

	def perform(self, command: MessageEnvelope) -> dict[str, object]:
		snapshot = _snapshot_for_perform(command)
		name = message_record_name(
			"inbox",
			snapshot["event_id"],
			snapshot["destination"],
		)
		now = now_datetime()
		token = secrets.token_urlsafe(32)
		if frappe.db.exists(self.message_box_definition.doctype, name):
			document = _lock_message(self.message_box_definition, name)
			_assert_existing_inbox_matches(document, snapshot)
			if document.status in {"Processed", "Ignored"}:
				return _inbox_replay_result(document)
			if document.status == "Processing" and get_datetime(document.lease_expires_at) > now:
				raise_ione_error("CONFLICT")
			if document.status == "Failed" and get_datetime(document.available_at) > now:
				raise_ione_error("CONFLICT")
			if cint(document.attempt_count) >= self.message_box_definition.max_attempts:
				raise_ione_error("INVALID_STATE_TRANSITION")
			document.status = "Processing"
			document.attempt_count = cint(document.attempt_count) + 1
			document.available_at = now
			document.claim_owner = snapshot["destination"]
			document.processing_token_hash = processing_token_hash(token)
			document.lease_expires_at = add_to_date(
				now,
				seconds=self.message_box_definition.default_lease_seconds,
			)
			document.completed_at = None
			document.last_error_code = None
			document.last_error_message = None
			with _message_write_scope(self.message_box_definition, "begin"):
				document.save(ignore_permissions=True)
			duplicate = True
		else:
			document = frappe.get_doc(
				{
					"doctype": self.message_box_definition.doctype,
					"name": name,
					**snapshot,
					"status": "Processing",
					"attempt_count": 1,
					"available_at": now,
					"claim_owner": snapshot["destination"],
					"processing_token_hash": processing_token_hash(token),
					"lease_expires_at": add_to_date(
						now,
						seconds=self.message_box_definition.default_lease_seconds,
					),
				}
			)
			try:
				with _message_write_scope(self.message_box_definition, "begin"):
					document.insert(ignore_permissions=True)
			except frappe.DuplicateEntryError as exc:
				raise_ione_error("CONFLICT", cause=exc)
			duplicate = False
		emit_audit_event(
			"transactional_inbox_started",
			logger_name="medops.transactional_message",
			box_kind="inbox",
			box_doctype=self.message_box_definition.doctype,
			box_status="Processing",
			attempt_count=cint(document.attempt_count),
			duplicate=duplicate,
		)
		return {
			"message": document.name,
			"status": document.status,
			"should_process": True,
			"duplicate": duplicate,
			"processing_token": token,
			"lease_expires_at": str(document.lease_expires_at),
		}


class CompleteInboxService(_MessageBoxService[CompleteMessage]):
	expected_kind = "inbox"

	def validate(self, command: CompleteMessage) -> None:
		self._validate_configuration()
		_validate_message_name(command.message_name)
		normalize_processing_token(command.processing_token)
		normalize_optional_result(command.result)

	def request_payload(self, command: CompleteMessage) -> dict[str, object]:
		result = normalize_optional_result(command.result)
		return {
			"message_name": _validate_message_name(command.message_name),
			"processing_token_hash": processing_token_hash(command.processing_token),
			"result": result[0] if result else None,
		}

	def perform(self, command: CompleteMessage) -> dict[str, object]:
		document = _lock_message(
			self.message_box_definition,
			_validate_message_name(command.message_name),
		)
		_assert_processing_token(document, command.processing_token)
		result = normalize_optional_result(command.result)
		document.status = "Processed"
		document.completed_at = now_datetime()
		document.result_json = result[1] if result else None
		document.result_hash = result[2] if result else None
		document.last_error_code = None
		document.last_error_message = None
		_clear_processing_lease(document)
		with _message_write_scope(self.message_box_definition, "complete"):
			document.save(ignore_permissions=True)
		emit_audit_event(
			"transactional_inbox_processed",
			logger_name="medops.transactional_message",
			box_kind="inbox",
			box_doctype=self.message_box_definition.doctype,
			box_status="Processed",
			attempt_count=cint(document.attempt_count),
		)
		return {
			"message": document.name,
			"status": document.status,
			"result": result[0] if result else None,
		}


class FailInboxService(_MessageBoxService[FailMessage]):
	expected_kind = "inbox"

	def validate(self, command: FailMessage) -> None:
		self._validate_configuration()
		_validate_message_name(command.message_name)
		normalize_processing_token(command.processing_token)
		normalize_error_details(command.error_code, command.error_message)
		_validate_retry_seconds(command.retry_after_seconds)

	def request_payload(self, command: FailMessage) -> dict[str, object]:
		error_code, error_message = normalize_error_details(
			command.error_code,
			command.error_message,
		)
		return {
			"message_name": _validate_message_name(command.message_name),
			"processing_token_hash": processing_token_hash(command.processing_token),
			"error_code": error_code,
			"error_message": error_message,
			"retry_after_seconds": _validate_retry_seconds(command.retry_after_seconds),
		}

	def perform(self, command: FailMessage) -> dict[str, object]:
		document = _lock_message(
			self.message_box_definition,
			_validate_message_name(command.message_name),
		)
		_assert_processing_token(document, command.processing_token)
		error_code, error_message = normalize_error_details(
			command.error_code,
			command.error_message,
		)
		now = now_datetime()
		ignored = cint(document.attempt_count) >= self.message_box_definition.max_attempts
		document.status = "Ignored" if ignored else "Failed"
		document.available_at = add_to_date(
			now,
			seconds=_validate_retry_seconds(command.retry_after_seconds),
		)
		document.completed_at = now if ignored else None
		document.last_error_code = error_code
		document.last_error_message = error_message
		_clear_processing_lease(document)
		with _message_write_scope(self.message_box_definition, "fail"):
			document.save(ignore_permissions=True)
		emit_audit_event(
			"transactional_inbox_failed",
			level="warning",
			logger_name="medops.transactional_message",
			box_kind="inbox",
			box_doctype=self.message_box_definition.doctype,
			box_status=document.status,
			attempt_count=cint(document.attempt_count),
			error_code=error_code,
		)
		return {
			"message": document.name,
			"status": document.status,
			"attempt_count": cint(document.attempt_count),
		}


def get_transactional_message_contract_status() -> TransactionalMessagePublicContract:
	with service_audit_scope():
		require_roles(MESSAGE_CONTRACT_ROLES)
		result = get_transactional_message_public_contract()
		emit_audit_event(
			"transactional_message_contract_read",
			logger_name="medops.transactional_message",
			contract_version=result["schema_version"],
			base_field_count=len(result["base_fields"]),
		)
		return result


__all__ = [
	"BeginInboxService",
	"ClaimOutboxMessage",
	"ClaimOutboxService",
	"CompleteInboxService",
	"CompleteMessage",
	"CompleteOutboxService",
	"FailInboxService",
	"FailMessage",
	"FailOutboxService",
	"MessageEnvelope",
	"PublishOutboxService",
	"TransactionalMessageDocument",
	"get_transactional_message_contract_status",
	"validate_message_box_doctype",
]
