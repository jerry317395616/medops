"""Concrete integration message contract and owned publication capability."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType

from medops.common.domain_service import normalize_sha256
from medops.common.organization import normalize_reference
from medops.common.transactional_message import MessageBoxDefinition

OUTBOX = "HRP Integration Outbox"
OUTBOX_DEFINITION = MessageBoxDefinition(
	doctype=OUTBOX,
	kind="outbox",
	required_roles=frozenset({"System Manager", "HRP System Manager", "HRP Budget Manager"}),
	max_attempts=10,
	default_lease_seconds=3600,
)
_publication: ContextVar[Mapping[str, object] | None] = ContextVar("medops_outbox_publication", default=None)


@contextmanager
def outbox_publication(expected_values: Mapping[str, object]):
	"""Called after domain authorization, never by a generic HTTP publisher."""
	fields = {
		"event_id",
		"event_type",
		"source",
		"destination",
		"payload_hash",
		"reference_doctype",
		"reference_name",
	}
	if set(expected_values) != fields or any(
		not isinstance(value, str) or not value or value != value.strip()
		for value in expected_values.values()
	):
		raise ValueError("complete owned message publication required")
	normalize_sha256(expected_values["payload_hash"], label="payload_hash")
	token = _publication.set(MappingProxyType(dict(expected_values)))
	try:
		yield
	finally:
		_publication.reset(token)


def publication_values() -> Mapping[str, object] | None:
	return _publication.get()


RETIREMENT_REASONS = MappingProxyType(
	{
		"MEDOPS-RELEASE-AUTHORIZATION-SUPERSEDED": "原授权已由新的明确确认取代，保留原消息供审计。",
		"MEDOPS-RELEASE-LEASE-EXHAUSTED": "最后一次处理租约到期，保留消息并等待重新授权。",
	}
)
RETIREMENT_ROLES = frozenset({"System Manager", "HRP System Manager"})


@dataclass(frozen=True, slots=True)
class OutboxRetirement:
	message: str
	payload_hash: str
	attempt: int
	status: str
	reason_code: str


_retirement: ContextVar[OutboxRetirement | None] = ContextVar("medops_outbox_retirement", default=None)


@contextmanager
def outbox_retirement(expected: OutboxRetirement):
	"""Issued only after the owning domain proves a current terminal predicate."""
	if (
		not isinstance(expected, OutboxRetirement)
		or expected.message != normalize_reference(expected.message, label="message")
		or expected.payload_hash != normalize_sha256(expected.payload_hash, label="payload_hash")
		or type(expected.attempt) is not int
		or not 0 <= expected.attempt <= OUTBOX_DEFINITION.max_attempts
		or expected.status not in {"Pending", "Failed", "Processing"}
		or expected.reason_code not in RETIREMENT_REASONS
	):
		raise ValueError("complete owned terminal predicate required")
	token = _retirement.set(expected)
	try:
		yield
	finally:
		_retirement.reset(token)


def retirement_values() -> OutboxRetirement | None:
	return _retirement.get()
