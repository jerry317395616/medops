from __future__ import annotations

import unittest

from medops.common.data_quality import (
	DataQualityContractError,
	build_data_quality_evaluate,
	build_data_quality_rule_upsert,
	evaluate_quality_value,
	issue_key_for,
	observed_value_digest,
	validate_rule_for_policy,
)
from medops.common.master_data import MASTER_DATA_TARGET_POLICIES


def valid_rule(**overrides: object) -> dict[str, object]:
	return {
		"code": "item-name-required",
		"display_name": "物料名称必填",
		"master_data_domain": "cod020-item",
		"company": "测试医疗法人",
		"hospital": "cod022-hospital",
		"target_field": "item_name",
		"rule_type": "Required",
		"valid_from": "2026-08-08",
		"remarks": " 质量治理规则 ",
		**overrides,
	}


class DataQualityContractTest(unittest.TestCase):
	def test_rule_upsert_normalizes_identity_scope_and_redacts_remarks(self) -> None:
		command = build_data_quality_rule_upsert(**valid_rule(enabled="1"))
		self.assertEqual(command.code, "ITEM-NAME-REQUIRED")
		self.assertEqual(command.master_data_domain, "COD020-ITEM")
		self.assertEqual(command.hospital, "COD022-HOSPITAL")
		self.assertEqual(command.parameters, {})
		self.assertTrue(command.enabled)
		self.assertEqual(len(command.rule_digest), 64)
		payload = command.as_request_payload()
		self.assertNotIn("质量治理规则", payload.values())
		self.assertEqual(len(str(payload["remarks_digest"])), 64)

	def test_rule_digest_is_stable_and_semantic(self) -> None:
		first = build_data_quality_rule_upsert(
			**valid_rule(
				rule_type="Allowed Values",
				parameters={"values": ["B", "A"]},
			)
		)
		repeat = build_data_quality_rule_upsert(
			**valid_rule(
				rule_type="Allowed Values",
				parameters='{"values":["A","B"]}',
			)
		)
		changed = build_data_quality_rule_upsert(
			**valid_rule(
				rule_type="Allowed Values",
				parameters={"values": ["A", "C"]},
			)
		)
		self.assertEqual(first.parameters, {"values": ["A", "B"]})
		self.assertEqual(first.rule_digest, repeat.rule_digest)
		self.assertNotEqual(first.rule_digest, changed.rule_digest)

	def test_rule_parameters_reject_executable_or_ambiguous_content(self) -> None:
		for overrides in (
			{"rule_type": "Required", "parameters": {"sql": "select 1"}},
			{"rule_type": "Named Pattern", "parameters": {"pattern": ".*"}},
			{"rule_type": "Named Pattern", "parameters": {"pattern_name": "CUSTOM"}},
			{"rule_type": "Maximum Length", "parameters": {"maximum": 501}},
			{"rule_type": "Allowed Values", "parameters": {"values": ["A", "A"]}},
			{"target_field": "api_secret"},
			{"enabled": "true"},
		):
			with self.subTest(overrides=overrides), self.assertRaises(DataQualityContractError):
				build_data_quality_rule_upsert(**valid_rule(**overrides))

	def test_update_requires_name_and_positive_revision_together(self) -> None:
		updated = build_data_quality_rule_upsert(
			**valid_rule(rule_name="ITEM-NAME-REQUIRED", expected_revision="2")
		)
		self.assertEqual(updated.expected_revision, 2)
		for overrides in (
			{"rule_name": None, "expected_revision": 1},
			{"rule_name": "ITEM-NAME-REQUIRED", "expected_revision": 0},
		):
			with self.subTest(overrides=overrides), self.assertRaises(DataQualityContractError):
				build_data_quality_rule_upsert(**valid_rule(**overrides))

	def test_policy_compatibility_uses_static_master_data_registry(self) -> None:
		policy = MASTER_DATA_TARGET_POLICIES["Item"]
		field = validate_rule_for_policy(build_data_quality_rule_upsert(**valid_rule()), policy)
		self.assertEqual(field.field_name, "item_name")
		for overrides in (
			{"target_field": "unknown_field"},
			{
				"target_field": "disabled",
				"rule_type": "Maximum Length",
				"parameters": {"maximum": 2},
			},
			{"target_field": "item_name", "rule_type": "Reference Exists"},
		):
			with self.subTest(overrides=overrides), self.assertRaises(DataQualityContractError):
				validate_rule_for_policy(
					build_data_quality_rule_upsert(**valid_rule(**overrides)),
					policy,
				)

	def test_declarative_evaluator_covers_all_rule_types(self) -> None:
		cases = (
			("Required", "{}", "", None, False, "REQUIRED_MISSING"),
			("Required", "{}", 0, None, True, None),
			("Allowed Values", '{"values":["A","B"]}', "C", None, False, "VALUE_NOT_ALLOWED"),
			("Maximum Length", '{"maximum":3}', "abcd", None, False, "MAXIMUM_LENGTH_EXCEEDED"),
			("Named Pattern", '{"pattern_name":"UPPER_CODE"}', "A-01", None, True, None),
			("Named Pattern", '{"pattern_name":"UPPER_CODE"}', "a-01", None, False, "NAMED_PATTERN_MISMATCH"),
			("Reference Exists", "{}", "UOM-1", False, False, "REFERENCE_NOT_FOUND"),
			("Reference Exists", "{}", "UOM-1", True, True, None),
		)
		for rule_type, parameters, value, reference_exists, passed, code in cases:
			with self.subTest(rule_type=rule_type, value=value):
				outcome = evaluate_quality_value(
					rule_type=rule_type,  # type: ignore[arg-type]
					parameters_json=parameters,
					value=value,
					reference_exists=reference_exists,
				)
				self.assertEqual(outcome.passed, passed)
				self.assertEqual(outcome.failure_code, code)

	def test_evaluation_command_requires_explicit_revision_and_date(self) -> None:
		command = build_data_quality_evaluate(
			rule_name="ITEM-NAME-REQUIRED",
			target_name="ITEM-001",
			effective_on="2026-08-08",
			expected_rule_revision="1",
		)
		self.assertEqual(command.expected_rule_revision, 1)
		for overrides in (
			{"effective_on": None},
			{"expected_rule_revision": 0},
		):
			with self.subTest(overrides=overrides), self.assertRaises(DataQualityContractError):
				payload = {
					"rule_name": "ITEM-NAME-REQUIRED",
					"target_name": "ITEM-001",
					"effective_on": "2026-08-08",
					"expected_rule_revision": 1,
					**overrides,
				}
				build_data_quality_evaluate(**payload)

	def test_issue_and_observed_digests_are_deterministic_and_directional(self) -> None:
		first = issue_key_for(
			rule_name="RULE-1",
			target_doctype="Item",
			target_name="ITEM-001",
		)
		repeat = issue_key_for(
			rule_name="RULE-1",
			target_doctype="Item",
			target_name="ITEM-001",
		)
		changed = issue_key_for(
			rule_name="RULE-1",
			target_doctype="Item",
			target_name="ITEM-002",
		)
		self.assertEqual(first, repeat)
		self.assertNotEqual(first, changed)
		self.assertNotEqual(observed_value_digest("secret-value"), observed_value_digest(None))


if __name__ == "__main__":
	unittest.main()
