"""Concrete integration message contract and owned publication capability."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType

from medops.common.domain_service import normalize_sha256
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
