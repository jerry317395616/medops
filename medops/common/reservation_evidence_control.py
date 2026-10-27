"""Internal, transaction-local allowance for one exact append-only evidence row.

This is not an authorization mechanism. The caller must independently prove
the real session, scope, source and transaction before entering the context.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType

from medops.common.reservation_authorization import reservation_authorization_row

_expected: ContextVar[Mapping[str, object] | None] = ContextVar(
	"medops_reservation_evidence_record", default=None
)


@contextmanager
def expected_reservation_evidence(
	intent: object, confirmation: object, decision: object | None = None
) -> Iterator[Mapping[str, object]]:
	if _expected.get() is not None:
		raise ValueError("nested reservation evidence write is not permitted")
	values = MappingProxyType(reservation_authorization_row(intent, confirmation, decision))
	token = _expected.set(values)
	try:
		yield values
	finally:
		_expected.reset(token)


def expected_evidence_values() -> Mapping[str, object] | None:
	return _expected.get()
