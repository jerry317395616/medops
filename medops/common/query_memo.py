"""Private adapter-injected read memoization; absent adapters always read anew."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol, TypeVar

T = TypeVar("T")


class QueryMemoizer(Protocol):
	def __call__(self, key: tuple, reader: Callable[[], T]) -> T: ...


_memoizer: ContextVar[QueryMemoizer | None] = ContextVar("medops_query_memoizer", default=None)


def memoized_query_value(key: tuple, reader: Callable[[], T]) -> T:
	provider = _memoizer.get()
	return reader() if provider is None else provider(key, reader)


@contextmanager
def use_query_memoizer(provider: QueryMemoizer) -> Iterator[None]:
	token = _memoizer.set(provider)
	try:
		yield
	finally:
		_memoizer.reset(token)
