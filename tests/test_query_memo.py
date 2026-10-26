from __future__ import annotations

import asyncio
import unittest

from medops.common.query_memo import memoized_query_value, use_query_memoizer


class TestQueryMemo(unittest.TestCase):
	def test_provider_is_isolated_between_concurrent_tasks(self):
		async def read(value):
			cache = {}

			def provider(key, reader):
				if key not in cache:
					cache[key] = reader()
				return cache[key]

			with use_query_memoizer(provider):
				memoized_query_value(("same-key",), lambda: value)
				await asyncio.sleep(0)
				return memoized_query_value(("same-key",), lambda: "unreachable")

		async def run():
			return await asyncio.gather(read("first"), read("second"))

		self.assertEqual(asyncio.run(run()), ["first", "second"])
		self.assertEqual(memoized_query_value(("same-key",), lambda: "fresh"), "fresh")

	def test_nested_provider_and_exception_restore_the_previous_adapter(self):
		def outer(_key, _reader):
			return "outer"

		def inner(_key, _reader):
			return "inner"

		with use_query_memoizer(outer):
			with self.assertRaisesRegex(RuntimeError, "controlled failure"), use_query_memoizer(inner):
				self.assertEqual(memoized_query_value((), lambda: "fresh"), "inner")
				raise RuntimeError("controlled failure")
			self.assertEqual(memoized_query_value((), lambda: "fresh"), "outer")
		self.assertEqual(memoized_query_value((), lambda: "fresh"), "fresh")
