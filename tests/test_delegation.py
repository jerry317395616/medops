from __future__ import annotations

import unittest

from medops.common.approval_matrix import ApprovalMatrixStep, ResolvedApprover, build_approval_matrix_step
from medops.common.delegation import (
	DelegationContractError,
	DelegationGrant,
	apply_delegation_grants,
	build_delegation_create,
	build_delegation_definition,
	build_delegation_revoke,
)


def _step(sequence: int = 1) -> ApprovalMatrixStep:
	return build_approval_matrix_step(
		sequence_no=sequence,
		step_name=f"审批步骤{sequence}",
		threshold_amount=0,
		approver_type="User",
		approver_user="owner@example.com",
		approval_mode="All",
	)


def _resolved(sequence: int = 1) -> list[tuple[ApprovalMatrixStep, list[ResolvedApprover]]]:
	return [
		(
			_step(sequence),
			[
				{
					"user": "owner@example.com",
					"source_type": "User",
					"source_value": "owner@example.com",
				}
			],
		)
	]


class DelegationContractTests(unittest.TestCase):
	def test_create_rejects_self_invalid_range_and_excessive_duration(self) -> None:
		base = {
			"matrix": "PO-MATRIX",
			"from_user": "owner@example.com",
			"to_user": "delegate@example.com",
			"valid_from": "2026-08-13",
			"valid_to": "2026-08-20",
		}
		for overrides in (
			{"to_user": "owner@example.com"},
			{"valid_to": "2026-08-12"},
			{"valid_to": "2027-08-14"},
			{"step_sequence": 0},
		):
			with self.subTest(overrides=overrides), self.assertRaises(DelegationContractError):
				build_delegation_create(**{**base, **overrides})

	def test_definition_digest_is_deterministic_and_locks_matrix_revision(self) -> None:
		values = {
			"matrix": "PO-MATRIX",
			"matrix_revision": 4,
			"matrix_digest": "a" * 64,
			"target_doctype": "Purchase Order",
			"company": "I-ONE",
			"hospital": "H001",
			"organization_unit": "FIN",
			"include_descendants": False,
			"from_user": "owner@example.com",
			"to_user": "delegate@example.com",
			"valid_from": "2026-08-13",
			"valid_to": "2026-08-20",
			"step_sequence": 2,
			"reason": "休假期间代办",
			"status": "Active",
		}
		left = build_delegation_definition(**values)
		right = build_delegation_definition(**dict(reversed(list(values.items()))))
		self.assertEqual(left.policy_digest, right.policy_digest)
		self.assertEqual(left.matrix_revision, 4)
		self.assertNotEqual(
			left.policy_digest,
			build_delegation_definition(**{**values, "matrix_revision": 5}).policy_digest,
		)

	def test_grant_replaces_only_matching_step_and_adds_audit_evidence(self) -> None:
		rows = [*_resolved(1), *_resolved(2)]
		result = apply_delegation_grants(
			rows,
			(
				DelegationGrant(
					name="DLG-2026-00001",
					from_user="owner@example.com",
					to_user="delegate@example.com",
					step_sequence=2,
				),
			),
		)
		self.assertEqual(result[0][1][0]["user"], "owner@example.com")
		self.assertEqual(result[1][1][0]["user"], "delegate@example.com")
		self.assertEqual(result[1][1][0].get("delegated_from"), "owner@example.com")
		self.assertEqual(result[1][1][0].get("delegation"), "DLG-2026-00001")

	def test_ambiguous_grants_fail_closed(self) -> None:
		with self.assertRaisesRegex(DelegationContractError, "multiple active delegations"):
			apply_delegation_grants(
				_resolved(),
				(
					DelegationGrant("DLG-1", "owner@example.com", "a@example.com", None),
					DelegationGrant("DLG-2", "owner@example.com", "b@example.com", 1),
				),
			)

	def test_revocation_requires_a_reason(self) -> None:
		with self.assertRaises(DelegationContractError):
			build_delegation_revoke(delegation="DLG-2026-00001", reason="")
		self.assertEqual(
			build_delegation_revoke(
				delegation="DLG-2026-00001",
				reason="提前返岗",
			).reason,
			"提前返岗",
		)


if __name__ == "__main__":
	unittest.main()
