from __future__ import annotations

import unittest
from decimal import Decimal

from medops.common.approval_matrix import (
	ApprovalMatrixContractError,
	ApprovalMatrixStep,
	ResolvedApprover,
	active_steps,
	build_approval_decision,
	build_approval_evaluation,
	build_approval_matrix_definition,
	build_approval_matrix_step,
	build_approval_matrix_steps,
)


def step(**overrides: object):
	values = {
		"sequence_no": 1,
		"step_name": "科室审批",
		"threshold_amount": 0,
		"approver_type": "Role",
		"approver_role": "HRP User",
		"approval_mode": "All",
	}
	values.update(overrides)
	return build_approval_matrix_step(**values)


def definition(*steps, dimensions: object = None):
	return build_approval_matrix_definition(
		code="po_main",
		display_name="采购审批矩阵",
		target_doctype="Purchase Order",
		company="I-ONE",
		hospital="H001",
		organization_unit="FIN",
		include_descendants=True,
		priority=10,
		dimensions=dimensions,
		enabled=True,
		valid_from="2026-01-01",
		revision=3,
		steps=steps or (step(),),
	)


def evaluation(*, amount: object = 100, dimensions: object = None):
	return build_approval_evaluation(
		doctype="Purchase Order",
		docname="PO-0001",
		amount=amount,
		dimensions=dimensions
		or {
			"company": "I-ONE",
			"hospital": "H001",
			"organization_unit": "FIN",
			"effective_on": "2026-08-13",
			"purchase_method": "公开招标",
		},
	)


class ApprovalMatrixContractTests(unittest.TestCase):
	def test_dimensions_are_declarative_bounded_and_canonical(self) -> None:
		left = definition(dimensions={"purchase_method": "公开招标", "risk_level": "High"})
		right = definition(dimensions={"risk_level": "High", "purchase_method": "公开招标"})
		self.assertEqual(left.policy_digest, right.policy_digest)
		for payload in (
			{"company": "OTHER"},
			{"purchase_method": ["公开招标"]},
			{"bad-key": "x"},
			'{"purchase_method": __import__("os")}',
		):
			with self.subTest(payload=payload), self.assertRaises(ApprovalMatrixContractError):
				definition(dimensions=payload)

	def test_step_parser_rejects_unknown_and_executable_fields(self) -> None:
		with self.assertRaises(ApprovalMatrixContractError):
			build_approval_matrix_steps(
				[
					{
						"sequence_no": 1,
						"step_name": "审批",
						"threshold_amount": 0,
						"approver_type": "Role",
						"approver_role": "HRP User",
						"script": "frappe.db.sql('DROP TABLE')",
					}
				]
			)

	def test_steps_must_start_at_zero_and_be_contiguous(self) -> None:
		for rows in (
			(step(threshold_amount=1),),
			(step(), step(sequence_no=3, step_name="院级审批", threshold_amount=1000)),
		):
			with self.subTest(rows=rows), self.assertRaises(ApprovalMatrixContractError):
				definition(*rows)

	def test_parallel_approvers_share_policy_and_cannot_repeat(self) -> None:
		role = step()
		user = step(approver_type="User", approver_role=None, approver_user="approver@example.com")
		self.assertEqual(len(definition(role, user).steps), 2)
		with self.assertRaises(ApprovalMatrixContractError):
			definition(role, role)
		with self.assertRaises(ApprovalMatrixContractError):
			definition(role, step(approval_mode="Any", approver_role="HRP Auditor"))

	def test_definition_digest_is_independent_of_parallel_row_order(self) -> None:
		role = step()
		user = step(approver_type="User", approver_role=None, approver_user="approver@example.com")
		self.assertEqual(definition(role, user).policy_digest, definition(user, role).policy_digest)

	def test_amount_activates_the_contiguous_threshold_chain(self) -> None:
		policy = definition(
			step(),
			step(sequence_no=2, step_name="院级审批", threshold_amount=1000, approver_role="HRP Auditor"),
			step(sequence_no=3, step_name="集团审批", threshold_amount=5000, approver_role="System Manager"),
		)
		self.assertEqual([row.sequence_no for row in active_steps(policy, Decimal("999.99"))], [1])
		self.assertEqual([row.sequence_no for row in active_steps(policy, Decimal("5000.00"))], [1, 2, 3])

	def test_decision_deduplicates_users_and_has_a_stable_digest(self) -> None:
		role = step()
		user = step(approver_type="User", approver_role=None, approver_user="a@example.com")
		policy = definition(role, user)
		query = evaluation()
		resolved: list[tuple[ApprovalMatrixStep, list[ResolvedApprover]]] = [
			(role, [{"user": "a@example.com", "source_type": "Role", "source_value": "HRP User"}]),
			(user, [{"user": "a@example.com", "source_type": "User", "source_value": "a@example.com"}]),
		]
		decision = build_approval_decision(
			evaluation=query,
			matrix="PO_MAIN",
			definition=policy,
			resolved_rows=resolved,
		)
		self.assertEqual(decision["approvers"], ["a@example.com"])
		self.assertEqual(len(decision["steps"][0]["approvers"]), 1)
		self.assertEqual(len(decision["decision_digest"]), 64)
		self.assertEqual(
			decision["decision_digest"],
			build_approval_decision(
				evaluation=query,
				matrix="PO_MAIN",
				definition=policy,
				resolved_rows=list(reversed(resolved)),
			)["decision_digest"],
		)

	def test_evaluation_requires_full_context_and_money_precision(self) -> None:
		with self.assertRaises(ApprovalMatrixContractError):
			evaluation(dimensions={"company": "I-ONE"})
		for amount in (-1, "1.001", "NaN"):
			with self.subTest(amount=amount), self.assertRaises(ApprovalMatrixContractError):
				evaluation(amount=amount)


if __name__ == "__main__":
	unittest.main()
