"""Memoize repeated source reads within one synchronous, read-only access graph.

Never wrap a command, transaction or mutation in this scope. Every public read,
locked reload and financial action must enter a new graph so a previous success
cannot authorize a later operation after a permission or source change.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")
_graph: ContextVar[dict[tuple, Any] | None] = ContextVar("medops_budget_access_graph", default=None)


def access_graph_cache():
	return _graph.get()


@contextmanager
def access_graph():
	current = _graph.get()
	if current is not None:
		yield current
		return
	token = _graph.set({})
	try:
		yield _graph.get()
	finally:
		_graph.reset(token)


def read_graph(function: Callable[P, R]) -> Callable[P, R]:
	@wraps(function)
	def checked(*args: P.args, **kwargs: P.kwargs) -> R:
		with access_graph():
			return function(*args, **kwargs)

	return checked
