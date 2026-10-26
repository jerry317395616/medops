from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from medops.hrp_budget.services.access_graph import access_graph, access_graph_cache, read_graph


class TestBudgetAccessGraph(unittest.TestCase):
	def test_each_top_level_read_is_fresh_and_cleans_up(self):
		@read_graph
		def read():
			cache = access_graph_cache()
			assert cache is not None
			self.assertEqual(cache, {})
			cache[("source",)] = True
			return cache

		self.assertIsNot(read(), read())
		self.assertIsNone(access_graph_cache())

	def test_nested_read_reuses_only_its_current_graph(self):
		@read_graph
		def nested():
			return access_graph_cache()

		with access_graph() as cache:
			self.assertIs(cache, nested())
			with access_graph() as inner:
				self.assertIs(inner, cache)
		self.assertIsNone(access_graph_cache())

	def test_exception_does_not_leave_authorization_in_next_call(self):
		with self.assertRaises(ValueError), access_graph() as cache:
			assert cache is not None
			cache[("allowed",)] = True
			raise ValueError("failed read")
		self.assertIsNone(access_graph_cache())
		with access_graph() as cache:
			self.assertEqual(cache, {})

	def test_threads_have_independent_authorization_graphs(self):
		def worker():
			self.assertIsNone(access_graph_cache())
			with access_graph() as cache:
				return cache

		with access_graph() as outer, ThreadPoolExecutor(max_workers=1) as executor:
			self.assertIsNot(executor.submit(worker).result(), outer)

	def test_decorator_preserves_arguments_return_value_and_identity(self):
		@read_graph
		def read(name, *, export=False):
			return name, export

		self.assertEqual(read("snapshot", export=True), ("snapshot", True))
		self.assertEqual(read.__name__, "read")
