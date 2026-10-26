from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from medops.hrp_budget.services.access_graph import access_graph, access_graph_cache, graph_value, read_graph


class TestBudgetAccessGraph(unittest.TestCase):
	def test_cached_denial_and_success_are_discarded_before_the_next_authority_phase(self):
		grants = {"manager": True, "auditor": False}
		calls = []

		def permission(user):
			calls.append(user)
			return grants[user]

		for expected in ((True, False), (False, True)):
			with access_graph():
				for _ in range(3):
					self.assertEqual(
						graph_value(("source", "manager"), lambda: permission("manager")), expected[0]
					)
					self.assertEqual(
						graph_value(("source", "auditor"), lambda: permission("auditor")), expected[1]
					)
			grants = {user: not value for user, value in grants.items()}
		self.assertEqual(calls, ["manager", "auditor", "manager", "auditor"])
		self.assertIsNone(access_graph_cache())

	def test_failed_reads_and_reads_outside_a_graph_never_reuse_prior_authority(self):
		calls = []

		def read():
			calls.append(True)
			if len(calls) == 1:
				raise ValueError("source unavailable")
			return len(calls)

		with access_graph():
			with self.assertRaises(ValueError):
				graph_value(("source",), read)
			self.assertEqual(graph_value(("source",), read), 2)
			self.assertEqual(graph_value(("source",), read), 2)
		self.assertEqual(graph_value(("source",), read), 3)
		self.assertEqual(graph_value(("source",), read), 4)

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
