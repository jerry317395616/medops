"""Transaction-local capabilities used only by owned financial domain services."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType

from medops.common.budget_ledger import LEDGER, RELEASE, RELEASE_ENTRY, BudgetReleaseFacts


@dataclass(frozen=True, slots=True)
class ReleaseMutation:
	operation: str
	facts: BudgetReleaseFacts
	expected_revision: int


@dataclass(frozen=True, slots=True)
class ExpectedReleaseRecord:
	doctype: str
	values: Mapping[str, object]


_mutation: ContextVar[ReleaseMutation | None] = ContextVar("medops_budget_release_mutation", default=None)
_record: ContextVar[ExpectedReleaseRecord | None] = ContextVar("medops_budget_release_record", default=None)


@contextmanager
def release_write(operation: str, facts: BudgetReleaseFacts, expected_revision: int):
	if (
		operation not in {"prepare", "revise", "preview", "confirm", "post"}
		or not isinstance(facts, BudgetReleaseFacts)
		or facts.operation not in {"Release", "Reverse"}
		or type(expected_revision) is not int
		or expected_revision < 0
		or (operation == "prepare" and expected_revision != 0)
		or (operation != "prepare" and expected_revision == 0)
	):
		raise ValueError("invalid owned budget release mutation")
	token = _mutation.set(ReleaseMutation(operation, facts, expected_revision))
	try:
		yield
	finally:
		_mutation.reset(token)


@contextmanager
def expected_release_record(doctype: str, values: Mapping[str, object]):
	mutation = _mutation.get()
	allowed = {
		RELEASE: {"prepare", "revise", "confirm", "post"},
		RELEASE_ENTRY: {"preview", "confirm"},
		LEDGER: {"post"},
	}
	if mutation is None or mutation.operation not in allowed.get(doctype, set()):
		raise ValueError("owned budget release mutation required")
	token = _record.set(ExpectedReleaseRecord(doctype, MappingProxyType(dict(values))))
	try:
		yield
	finally:
		_record.reset(token)


def release_mutation() -> ReleaseMutation | None:
	return _mutation.get()


def release_record() -> ExpectedReleaseRecord | None:
	return _record.get()
