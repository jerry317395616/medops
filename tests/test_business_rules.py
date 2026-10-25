from __future__ import annotations

import unittest

from medops.common.business_rules import (
	BusinessRuleError,
	EvaluationBudget,
	evaluate_tree,
	field_paths,
	normalize_tree,
	policy_digest,
	rule_payload,
	validate_tree_contract,
)


def contract(kind="number", fieldtype="Currency", options: str | list[str] = "currency"):
	return {"kind": kind, "fieldtype": fieldtype, "options": options, "permlevel": 0}


class BusinessRuleTests(unittest.TestCase):
	def evaluate(self, predicate, values, contracts, *, budget=None):
		tree = normalize_tree(predicate)
		validate_tree_contract(tree, contracts)
		return evaluate_tree(tree, values, contracts, budget=budget or EvaluationBudget())

	def test_decimal_comparison_and_cross_field_bounds(self):
		c = {"amount": contract(), "limit": contract()}
		p = {
			"all": [
				{"field": "amount", "op": "eq", "value": "0.30"},
				{"field": "amount", "op": "lte", "other_field": "limit"},
			]
		}
		self.assertTrue(self.evaluate(p, {"amount": "0.3", "limit": ".30"}, c))
		self.assertFalse(self.evaluate(p, {"amount": "0.300001", "limit": ".30"}, c))

	def test_boolean_and_missing_values_do_not_fail_open(self):
		c = {"enabled": contract("boolean", "Check", "")}
		self.assertTrue(self.evaluate({"field": "enabled", "op": "required"}, {"enabled": 0}, c))
		self.assertFalse(self.evaluate({"field": "enabled", "op": "ne", "value": True}, {}, c))
		self.assertTrue(self.evaluate({"field": "enabled", "op": "empty"}, {}, c))

	def test_date_order_and_select_set(self):
		c = {
			"from_date": contract("date", "Date", ""),
			"to_date": contract("date", "Date", ""),
			"status": contract("text", "Select", ["Draft", "Approved"]),
		}
		p = {
			"all": [
				{"field": "from_date", "op": "lte", "other_field": "to_date"},
				{"field": "status", "op": "in", "value": ["Draft"]},
			]
		}
		self.assertTrue(
			self.evaluate(p, {"from_date": "2026-01-01", "to_date": "2026-01-02", "status": "Draft"}, c)
		)
		with self.assertRaises(BusinessRuleError):
			self.evaluate({"field": "status", "op": "eq", "value": "SQL"}, {}, c)

	def test_child_quantifiers_require_nonempty_and_validate_every_row(self):
		c = {"items": contract("table", "Table", "Item Row"), "items.qty": contract("number", "Float", "")}
		p = {"table": "items", "quantifier": "all", "where": {"field": "qty", "op": "gt", "value": 0}}
		self.assertFalse(self.evaluate(p, {"items": []}, c))
		self.assertFalse(self.evaluate(p, {"items": [{"qty": 2}, {"qty": 0}]}, c))
		self.assertTrue(self.evaluate({**p, "quantifier": "any"}, {"items": [{"qty": 2}, {"qty": 0}]}, c))
		self.assertEqual(field_paths(normalize_tree(p)), {"items", "items.qty"})

	def test_no_executable_expression_unknown_keys_or_nested_tables(self):
		for p in [
			"doc.amount > 0",
			{"field": "__class__", "op": "eq", "value": "x"},
			{"field": "amount", "op": "eval", "value": "True"},
			{"field": "amount", "op": "required", "sql": "select 1"},
			{
				"table": "items",
				"quantifier": "all",
				"where": {
					"table": "nested",
					"quantifier": "all",
					"where": {"field": "qty", "op": "required"},
				},
			},
		]:
			with self.subTest(predicate=p), self.assertRaises(ValueError):
				normalize_tree(p)

	def test_depth_row_operation_and_numeric_limits(self):
		p = {"field": "amount", "op": "gt", "value": 0}
		with self.assertRaises(BusinessRuleError):
			self.evaluate(p, {"amount": "1e25"}, {"amount": contract()})
		with self.assertRaises(BusinessRuleError):
			self.evaluate(p, {"amount": True}, {"amount": contract()})
		for _ in range(7):
			p = {"all": [p]}
		with self.assertRaises(BusinessRuleError):
			normalize_tree(p)
		with self.assertRaises(BusinessRuleError):
			self.evaluate(
				{"all": [{"field": "amount", "op": "required"}]},
				{"amount": 1},
				{"amount": contract()},
				budget=EvaluationBudget(1),
			)

	def test_currency_units_cannot_be_compared_implicitly(self):
		p = {"field": "amount", "op": "lte", "other_field": "base_amount"}
		with self.assertRaises(BusinessRuleError):
			self.evaluate(p, {}, {"amount": contract(), "base_amount": contract(options="company_currency")})

	def test_policy_unknown_fields_dates_and_digest(self):
		p = {
			"code": "TEST-RULE",
			"display_name": "Test",
			"target_doctype": "Request",
			"target_action": "submit",
			"company": "Company",
			"hospital": "Hospital",
			"severity": "Block",
			"valid_from": "2026-01-01",
			"assertion_json": {"field": "amount", "op": "required"},
		}
		n = rule_payload(p)
		self.assertEqual(n, rule_payload(n))
		self.assertEqual(
			policy_digest(n, {"amount": contract()}),
			policy_digest(rule_payload(dict(reversed(list(p.items())))), {"amount": contract()}),
		)
		self.assertNotEqual(
			policy_digest(n, {"amount": contract()}),
			policy_digest(n, {"amount": {**contract(), "permlevel": 1}}),
		)
		for extra in [
			{"condition": "True"},
			{"valid_to": "2025-01-01"},
			{"include_descendants": 1},
			{"target_action": "exec"},
		]:
			with self.subTest(extra=extra), self.assertRaises(BusinessRuleError):
				rule_payload({**p, **extra})

	def test_row_limit_and_timezone_constants_are_rejected(self):
		c = {"items": contract("table", "Table", "Rows"), "items.qty": contract("number", "Float", "")}
		with self.assertRaises(BusinessRuleError):
			self.evaluate(
				{"table": "items", "quantifier": "all", "where": {"field": "qty", "op": "required"}},
				{"items": [{"qty": 1}] * 2001},
				c,
			)
		with self.assertRaises(BusinessRuleError):
			self.evaluate(
				{"field": "at", "op": "gt", "value": "2026-01-01T00:00:00+08:00"},
				{},
				{"at": contract("datetime", "Datetime", "")},
			)
