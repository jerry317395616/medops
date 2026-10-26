from __future__ import annotations

import unittest
from decimal import localcontext
from typing import Any
from unittest.mock import patch

from medops.common.budget_ledger import LEDGER_PUBLIC_FIELDS
from medops.common.budget_statement import build_statement_request, complete_statement, statement_dates


def context(**values: Any) -> dict[str, Any]:
	return dict(
		company="C",
		hospital="H",
		organization_version="V",
		organization_unit="A",
		fiscal_year="2026",
		currency="CNY",
		from_date="2026-01-01",
		to_date="2026-12-31",
		fiscal_year_start="2026-01-01",
		fiscal_year_end="2026-12-31",
		**values,
	)


def entry(
	name="e1",
	*,
	amount="1.00",
	debit="1.00",
	credit="0.00",
	precision=2,
	posting_date="2026-01-01",
	**values: Any,
) -> dict[str, Any]:
	row: dict[str, Any] = dict.fromkeys(LEDGER_PUBLIC_FIELDS)
	row.update(
		{
			key: context()[key]
			for key in (
				"company",
				"hospital",
				"organization_version",
				"organization_unit",
				"fiscal_year",
				"currency",
			)
		}
	)
	row.update(
		name=name,
		amount=amount,
		debit=debit,
		credit=credit,
		currency_precision=precision,
		amount_schema_version=2,
		posting_date=posting_date,
		posting_time="10:00:00",
		is_reversal=0,
	)
	row.update(values)
	return row


class TestBudgetStatement(unittest.TestCase):
	def test_mixed_captured_precisions_are_exact_under_a_small_ambient_context(self):
		with localcontext() as decimal_context:
			decimal_context.prec = 5
			book = complete_statement(
				context(),
				[
					entry(
						amount="999999999999999.999999999",
						debit="999999999999999.999999999",
						credit="0.000000000",
						precision=9,
					),
					entry("e2", amount="1", debit="1", credit="0", precision=0),
				],
			)
		self.assertEqual(book.totals["closing"], "1000000000000000.999999999")
		self.assertEqual(book.totals["precision"], 9)
		self.assertEqual(book.rows[-1]["running_balance"], book.totals["closing"])

	def test_opening_period_and_full_reversal_have_no_negative_zero(self):
		query = dict(context(), from_date="2026-02-01")
		book = complete_statement(
			query,
			[
				entry(),
				entry(
					"e2",
					amount="-1.00",
					debit="0.00",
					credit="1.00",
					posting_date="2026-02-02",
					is_reversal=1,
				),
			],
		)
		self.assertEqual(
			dict(book.totals),
			dict(precision=2, opening="1.00", debit="0.00", credit="1.00", net="-1.00", closing="0.00"),
		)
		self.assertEqual(len(book.entries), 2)
		self.assertEqual(len(book.rows), 1)
		self.assertEqual(book.rows[0]["running_balance"], "0.00")

	def test_digest_is_order_independent_but_binds_opening_facts_and_context(self):
		rows = [entry(), entry("e2", posting_date="2026-02-01")]
		book = complete_statement(context(), rows)
		self.assertEqual(book.digest, complete_statement(context(), list(reversed(rows))).digest)
		self.assertNotEqual(
			book.digest, complete_statement(dict(context(), from_date="2026-02-01"), rows).digest
		)
		self.assertNotEqual(
			book.digest, complete_statement(context(), [entry(amount="2.00", debit="2.00"), rows[1]]).digest
		)

	def test_private_or_missing_fields_never_enter_the_projection(self):
		for row in (
			dict(entry(), voucher_no="hidden-batch"),
			{key: value for key, value in entry().items() if key != "amount"},
		):
			with self.assertRaises(ValueError):
				complete_statement(context(), [row])

	def test_context_and_accounting_mismatch_are_rejected(self):
		invalid: list[dict[str, Any]] = [
			{"organization_unit": "B"},
			{"currency": "USD"},
			{"amount_schema_version": 1},
			{"debit": "2.00"},
			{"credit": "-1.00"},
			{"amount": 1.0},
			{"currency_precision": True},
		]
		for changes in invalid:
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				complete_statement(context(), [entry(**changes)])

	def test_bounds_duplicate_and_outside_date_rows_are_rejected(self):
		with patch("medops.common.budget_statement.MAX_STATEMENT_ENTRIES", 1), self.assertRaises(ValueError):
			complete_statement(context(), [entry(), entry("e2")])
		for rows in (
			[entry(), entry()],
			[entry(posting_date="2025-12-31")],
			[entry(posting_date="2027-01-01")],
		):
			with self.assertRaises(ValueError):
				complete_statement(context(), rows)

	def test_request_has_fixed_fields_bounded_pages_and_digest_for_following_pages(self):
		payload = dict(organization_unit="A", fiscal_year="2026", currency="CNY")
		self.assertEqual(build_statement_request(payload).limit, 50)
		with self.assertRaises(ValueError):
			build_statement_request(payload, export=True)
		self.assertEqual(build_statement_request(payload, offset=50, expected_digest="a" * 64).offset, 50)
		invalid: list[dict[str, Any]] = [
			{"offset": 1},
			{"limit": 51},
			{"limit": 0},
			{"offset": True},
			{"expected_digest": "bad"},
		]
		for values in invalid:
			with self.subTest(values=values), self.assertRaises(ValueError):
				build_statement_request(payload, **values)
		with self.assertRaises(ValueError):
			build_statement_request(dict(payload, filters={"company": "unreviewed"}))

	def test_fiscal_dates_are_explicit_and_cannot_escape_the_year(self):
		payload = dict(organization_unit="A", fiscal_year="2026", currency="CNY")
		request = build_statement_request(payload)
		self.assertEqual(
			statement_dates(request.query, "2026-01-01", "2026-12-31"), ("2026-01-01", "2026-12-31")
		)
		for values in (
			{"from_date": "2025-12-31"},
			{"to_date": "2027-01-01"},
			{"from_date": "bad"},
			{"from_date": "2026-02-01", "to_date": "2026-01-01"},
		):
			with self.assertRaises(ValueError):
				statement_dates(
					build_statement_request(dict(payload, **values)).query, "2026-01-01", "2026-12-31"
				)

	def test_empty_book_is_explicit_and_result_mappings_cannot_be_mutated(self):
		book = complete_statement(context(), [])
		self.assertEqual(book.rows, ())
		self.assertEqual(book.totals["closing"], "0")
		with self.assertRaises(TypeError):
			mutable: Any = book.totals
			mutable["closing"] = "1"
		with self.assertRaises(TypeError):
			mutable = complete_statement(context(), [entry()]).rows[0]
			mutable["amount"] = "2.00"

	def test_all_captured_precisions_keep_row_strings(self):
		for precision in range(10):
			zero = "0" if precision == 0 else "0." + "0" * precision
			amount = "1" if precision == 0 else "1." + "0" * precision
			with self.subTest(precision=precision):
				book = complete_statement(
					context(), [entry(amount=amount, debit=amount, credit=zero, precision=precision)]
				)
				self.assertEqual(book.rows[0]["amount"], amount)
				self.assertEqual(book.totals["closing"], amount)
