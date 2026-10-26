"""Memory-only ownership of a statement read connection and native base checks."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class StatementRead:
	actor: str = field(repr=False)
	connection: object = field(repr=False)


_read: ContextVar[StatementRead | None] = ContextVar("medops_budget_statement_read", default=None)
_native_base: ContextVar[bool] = ContextVar("medops_budget_ledger_native_base", default=False)


@contextmanager
def statement_read(scope: StatementRead):
	if _read.get() is not None or not isinstance(scope, StatementRead):
		raise ValueError("a new owned statement read is required")
	token = _read.set(scope)
	try:
		yield
	finally:
		_read.reset(token)


def current_statement_read():
	return _read.get()


@contextmanager
def native_ledger_base_check():
	token = _native_base.set(True)
	try:
		yield
	finally:
		_native_base.reset(token)


def use_native_ledger_defaults():
	return _native_base.get()
