from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any

from medops.common.approval_matrix import (
	ApprovalEvaluation,
	ApprovalMatrixContractError,
	ApprovalMatrixDefinition,
	ApprovalMatrixStep,
	ResolvedApprover,
	active_steps,
	build_approval_decision,
	build_approval_evaluation,
	build_approval_matrix_definition,
	build_approval_matrix_steps,
)
from medops.common.delegation import DelegationGrant, apply_delegation_grants

GOLDEN = json.loads(
	(Path(__file__).parent / "fixtures/approval_v1_contract.json").read_text(encoding="utf-8")
)


def policy(*rows, precision=9, version=2):
	if not rows:
		rows = (
			{
				"sequence_no": 1,
				"step_name": "Review",
				"threshold_amount": "0",
				"approver_type": "Role",
				"approver_role": "HRP User",
			},
		)
	return build_approval_matrix_definition(
		code="EXACT_BUDGET",
		display_name="精确预算审批",
		target_doctype="HRP Budget Approval",
		company="I-ONE",
		hospital="HOSPITAL-A",
		organization_unit="I-ONE-FIN",
		valid_from="2026-01-01",
		steps=build_approval_matrix_steps(list(rows), schema_version=version, amount_precision=precision),
		schema_version=version,
		amount_precision=precision,
	)


def query(amount: object = "0", precision=9, **overrides: Any):
	values: dict[str, Any] = {
		"doctype": "HRP Budget Approval",
		"docname": "CASE-1",
		"amount": amount,
		"schema_version": 2,
		"amount_precision": precision,
		"dimensions": {
			"company": "I-ONE",
			"hospital": "HOSPITAL-A",
			"organization_unit": "I-ONE-FIN",
			"effective_on": "2026-01-01",
		},
	}
	values.update(overrides)
	return build_approval_evaluation(**values)


def decide(
	definition: ApprovalMatrixDefinition,
	evaluation: ApprovalEvaluation,
	rows: list[tuple[ApprovalMatrixStep, list[ResolvedApprover]]] | None = None,
):
	if rows is None:
		rows = [
			(
				row,
				[
					{
						"user": "reviewer@example.com",
						"source_type": row.approver_type,
						"source_value": row.source_value,
					}
				],
			)
			for row in active_steps(definition, evaluation.amount)
		]
	return build_approval_decision(
		evaluation=evaluation, matrix="EXACT_BUDGET", definition=definition, resolved_rows=rows
	)


def no_floats(test, value):
	test.assertNotIsInstance(value, float)
	if isinstance(value, dict):
		for child in value.values():
			no_floats(test, child)
	elif isinstance(value, list):
		for child in value:
			no_floats(test, child)


class TestExactApprovalContract(unittest.TestCase):
	def test_version1_policy_request_and_decision_match_prechange_golden(self):
		definition_payload = GOLDEN["policy"].copy()
		definition_payload.pop("policy_digest")
		definition_payload["steps"] = build_approval_matrix_steps(definition_payload["steps"])
		definition = build_approval_matrix_definition(**definition_payload)
		evaluation = build_approval_evaluation(**GOLDEN["evaluation"])
		rows = [
			(row, GOLDEN["decision"]["steps"][index]["approvers"])
			for index, row in enumerate(definition.steps)
		]
		self.assertEqual(definition.as_public_dict(), GOLDEN["policy"])
		self.assertEqual(evaluation.as_request_payload(), GOLDEN["evaluation"])
		self.assertEqual(
			build_approval_decision(
				evaluation=evaluation, matrix="PO_MAIN", definition=definition, resolved_rows=rows
			),
			GOLDEN["decision"],
		)

	def test_exact_large_threshold_and_decision_never_encode_floats(self):
		maximum = "999999999999999.999999999"
		definition = policy(
			{
				"sequence_no": 1,
				"step_name": "Review",
				"threshold_amount": "0",
				"approver_type": "Role",
				"approver_role": "HRP User",
			},
			{
				"sequence_no": 2,
				"step_name": "Final",
				"threshold_amount": maximum,
				"approver_type": "Role",
				"approver_role": "HRP Auditor",
			},
		)
		result = decide(definition, query(maximum))
		self.assertEqual(result.get("amount"), maximum)
		self.assertEqual(result["steps"][1]["threshold_amount"], maximum)
		no_floats(self, definition.as_public_dict())
		no_floats(self, result)

	def test_all_captured_precisions_compare_at_exact_threshold(self):
		for precision in range(10):
			quantum = Decimal(1).scaleb(-precision)
			definition = policy(
				{
					"sequence_no": 1,
					"step_name": "Review",
					"threshold_amount": "0",
					"approver_type": "Role",
					"approver_role": "HRP User",
				},
				{
					"sequence_no": 2,
					"step_name": "Final",
					"threshold_amount": str(quantum),
					"approver_type": "Role",
					"approver_role": "HRP Auditor",
				},
				precision=precision,
			)
			for amount, steps in [(Decimal(0), 1), (quantum, 2), (quantum * 2, 2)]:
				with self.subTest(precision=precision, amount=amount):
					result = decide(definition, query(str(amount), precision))
					self.assertEqual(len(result["steps"]), steps)

	def test_exact_inputs_reject_binary_floats_and_nonfinite_or_negative_values(self):
		for amount in (1.0, 0.1, True, "NaN", "Infinity", "-0.1", "0.0000000001", "1" * 65):
			with self.subTest(amount=amount), self.assertRaises(ApprovalMatrixContractError):
				query(amount)

	def test_amount_profiles_are_bounded_and_legacy_stays_two_digits(self):
		for version, precision in [(True, 2), (3, 2), (2, True), (2, -1), (2, 10), (1, 9)]:
			with (
				self.subTest(version=version, precision=precision),
				self.assertRaises(ApprovalMatrixContractError),
			):
				policy(version=version, precision=precision)

	def test_matrix_cannot_mix_step_precision_profiles(self):
		with self.assertRaises(ApprovalMatrixContractError):
			build_approval_matrix_definition(
				code="MIX",
				display_name="Mixed",
				target_doctype="HRP Budget Approval",
				company="I-ONE",
				hospital="HOSPITAL-A",
				valid_from="2026-01-01",
				steps=policy().steps,
				schema_version=2,
				amount_precision=2,
			)

	def test_exact_budget_targets_reject_legacy_evaluation(self):
		with self.assertRaises(ApprovalMatrixContractError):
			query(schema_version=1, amount_precision=2)

	def test_exact_policy_requires_matching_evaluation_precision(self):
		with self.assertRaises(ApprovalMatrixContractError):
			decide(policy(precision=9), query(precision=2))
		legacy_query = build_approval_evaluation(**GOLDEN["evaluation"])
		with self.assertRaises(ApprovalMatrixContractError):
			decide(policy(), legacy_query)

	def test_exact_decision_binds_amount_and_context(self):
		definition = policy()
		first = decide(definition, query("0.100000001"))
		second = decide(definition, query("0.100000002"))
		self.assertNotEqual(first.get("context_digest"), second.get("context_digest"))
		self.assertNotEqual(first["decision_digest"], second["decision_digest"])

	def test_legacy_policy_can_supply_exact_thresholds_without_rewriting_digest(self):
		payload = GOLDEN["policy"].copy()
		payload.pop("policy_digest")
		payload["steps"] = build_approval_matrix_steps(payload["steps"])
		definition = build_approval_matrix_definition(**payload)
		evaluation = build_approval_evaluation(
			**{**GOLDEN["evaluation"], "schema_version": 2, "amount_precision": 9, "amount": "1234.500000001"}
		)
		result = decide(definition, evaluation)
		self.assertEqual(result["policy_digest"], GOLDEN["policy"]["policy_digest"])
		self.assertEqual(result.get("policy_schema_version"), 1)
		self.assertEqual(result["steps"][1]["threshold_amount"], "1000")
		no_floats(self, result)

	def test_all_original_sources_and_delegation_principals_survive_user_deduplication(self):
		definition = policy(
			{
				"sequence_no": 1,
				"step_name": "Review",
				"threshold_amount": "0",
				"approver_type": "Role",
				"approver_role": "HRP User",
			},
			{
				"sequence_no": 1,
				"step_name": "Review",
				"threshold_amount": "0",
				"approver_type": "User",
				"approver_user": "principal@example.com",
			},
		)
		rows: list[tuple[ApprovalMatrixStep, list[ResolvedApprover]]] = [
			(
				definition.steps[0],
				[{"user": "reviewer@example.com", "source_type": "Role", "source_value": "HRP User"}],
			),
			(
				definition.steps[1],
				[
					{
						"user": "reviewer@example.com",
						"source_type": "User",
						"source_value": "principal@example.com",
						"delegated_from": "principal@example.com",
						"delegation": "DELEGATION-1",
					}
				],
			),
		]
		result = decide(definition, query(), rows)
		self.assertEqual(result["approvers"], ["reviewer@example.com"])
		self.assertEqual(len(result["steps"][0].get("source_assignments", [])), 2)
		self.assertEqual(result["delegations"], ["DELEGATION-1"])
		self.assertEqual(
			result["decision_digest"], decide(definition, query(), list(reversed(rows)))["decision_digest"]
		)
		with self.assertRaises(ApprovalMatrixContractError):
			decide(definition, query(), rows[:1])

	def test_canonical_amount_strings_and_precision_are_both_bound_to_policy(self):
		def rows(amount):
			return [
				{
					"sequence_no": 1,
					"step_name": "Review",
					"threshold_amount": "0",
					"approver_type": "Role",
					"approver_role": "HRP User",
				},
				{
					"sequence_no": 2,
					"step_name": "Final",
					"threshold_amount": amount,
					"approver_type": "Role",
					"approver_role": "HRP Auditor",
				},
			]

		self.assertEqual(policy(*rows("1.2300")).policy_digest, policy(*rows("1.23")).policy_digest)
		self.assertNotEqual(
			policy(*rows("1.23"), precision=2).policy_digest, policy(*rows("1.23"), precision=9).policy_digest
		)

	def test_shared_delegate_keeps_every_principal_and_has_order_independent_digest(self):
		definition = policy()
		step = definition.steps[0]
		original: list[tuple[ApprovalMatrixStep, list[ResolvedApprover]]] = [
			(
				step,
				[
					{"user": user, "source_type": "Role", "source_value": "HRP User"}
					for user in ("a@example.com", "b@example.com")
				],
			)
		]
		grants = tuple(
			DelegationGrant(
				name=f"D-{index}", from_user=user, to_user="delegate@example.com", step_sequence=1
			)
			for index, user in enumerate(("a@example.com", "b@example.com"))
		)
		resolved = apply_delegation_grants(original, grants)
		first = decide(definition, query(), resolved)
		second = decide(definition, query(), [(step, list(reversed(resolved[0][1])))])
		self.assertEqual(first["decision_digest"], second["decision_digest"])
		self.assertEqual(first["delegations"], ["D-0", "D-1"])
		self.assertEqual(
			[source.get("delegated_from") for source in first["steps"][0].get("source_assignments", [])],
			["a@example.com", "b@example.com"],
		)


if __name__ == "__main__":
	unittest.main()
