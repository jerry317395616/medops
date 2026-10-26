from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from typing import Any

from medops.common.budget_ledger import RELEASE_PUBLIC_FIELDS
from medops.common.budget_release_directory import (
	SOURCE_PUBLIC_FIELDS,
	build_release_directory,
	directory_digest,
)


def filters(**values):
	return dict(company="C", hospital="H", fiscal_year="2026", **values)


def row(kind="records", name="record", **values) -> dict[str, Any]:
	result: dict[str, Any] = dict.fromkeys(
		RELEASE_PUBLIC_FIELDS if kind == "records" else SOURCE_PUBLIC_FIELDS
	)
	result.update(name=name, total_amount="999999999999999.123456789", currency="CNY")
	result.update(values)
	return result


class TestBudgetReleaseDirectory(unittest.TestCase):
	def test_context_is_mandatory_and_arbitrary_filters_and_sql_operators_are_rejected(self):
		for kind in ("records", "eligible"):
			for data in (
				{},
				{"company": "C"},
				dict(filters(), doctype="User"),
				dict(filters(), company=["like", "%"]),
			):
				with self.subTest(kind=kind, data=data), self.assertRaises(ValueError):
					build_release_directory(kind, data)
		with self.assertRaises(ValueError):
			build_release_directory("unknown", filters())

	def test_only_reviewed_statuses_operations_and_kind_specific_fields_are_accepted(self):
		command = build_release_directory(
			"records", filters(status="Authorized", operation="Reverse", reversal_of="r")
		)
		self.assertEqual(dict(command.filters)["reversal_of"], "r")
		for data in (filters(status="Cancelled"), filters(operation="Pay")):
			with self.assertRaises(ValueError):
				build_release_directory("records", data)
		for key in ("status", "operation", "source_approval", "reversal_of"):
			with self.assertRaises(ValueError):
				build_release_directory("eligible", dict(filters(), **{key: "r"}))

	def test_pagination_is_bounded_and_later_pages_require_a_digest(self):
		self.assertEqual(build_release_directory("records", filters()).limit, 50)
		self.assertEqual(
			build_release_directory(
				"records", filters(), offset=1000, limit=1, expected_digest="a" * 64
			).offset,
			1000,
		)
		for values in (
			{"offset": 1},
			{"offset": True},
			{"offset": 1001},
			{"limit": 0},
			{"limit": 51},
			{"expected_digest": "bad"},
		):
			with self.subTest(values=values), self.assertRaises(ValueError):
				build_release_directory("records", filters(), **values)

	def test_digest_binds_kind_context_order_and_every_visible_fact(self):
		command = build_release_directory("records", filters())
		digest = directory_digest(command, [row(), row(name="second")])
		self.assertRegex(digest, r"^[0-9a-f]{64}$")
		self.assertNotEqual(digest, directory_digest(command, [row(name="second"), row()]))
		self.assertNotEqual(digest, directory_digest(command, [row(total_amount="1"), row(name="second")]))
		self.assertNotEqual(
			digest,
			directory_digest(
				build_release_directory("records", filters(currency="CNY")), [row(), row(name="second")]
			),
		)
		self.assertNotEqual(
			digest,
			directory_digest(
				build_release_directory("eligible", filters()),
				[row("eligible"), row("eligible", name="second")],
			),
		)

	def test_private_fields_duplicates_and_incomplete_projections_cannot_enter_digest(self):
		command = build_release_directory("records", filters())
		for rows in ([row(outbox_message="private")], [row(), row()], [{"name": "missing"}]):
			with self.subTest(rows=rows), self.assertRaises(ValueError):
				directory_digest(command, rows)

	def test_full_directory_hash_streams_beyond_a_single_service_result_size(self):
		command = build_release_directory("records", filters())
		rows = [row(name=str(number), reason="字面理由" * 100) for number in range(1000)]
		self.assertEqual(directory_digest(command, rows), directory_digest(command, rows))
		self.assertEqual(rows[0]["total_amount"], "999999999999999.123456789")
		with self.assertRaises(ValueError):
			directory_digest(command, [*rows, row(name="overflow")])

	def test_command_is_immutable_and_filter_input_order_is_canonical(self):
		command = build_release_directory("records", filters())
		self.assertEqual(command, build_release_directory("records", dict(reversed(list(filters().items())))))
		with self.assertRaises(FrozenInstanceError):
			frozen: Any = command
			frozen.offset = 1
