from __future__ import annotations

import unittest
from copy import deepcopy
from typing import Any

from medops.common.budget_workflow import (
	CASE,
	Vote,
	build_case_request,
	build_human_confirmation,
	build_review_intent,
	parse_decision,
	replay_votes,
)
from medops.common.domain_service import fingerprint_json


def decision(mode="All", delegated=False) -> dict[str, Any]:
	def source(user):
		return {"user": user, "source_type": "User", "source_value": user}

	first = [source("a@example.com"), source("b@example.com")]
	if delegated:
		first[0].update(delegated_from="preparer@example.com", delegation="Grant")
	value = {
		"schema_version": 2,
		"doctype": CASE,
		"docname": "Case",
		"amount": "1.000000001",
		"amount_precision": 9,
		"matrix": "Matrix",
		"matrix_code": "MATRIX",
		"matrix_name": "Budget review",
		"policy_version": 1,
		"policy_digest": "a" * 64,
		"context_digest": "b" * 64,
		"policy_schema_version": 2,
		"delegations": ["Grant"] if delegated else [],
		"approvers": ["a@example.com", "b@example.com", "c@example.com"],
		"steps": [
			{
				"sequence_no": 1,
				"approval_mode": mode,
				"approvers": first,
				"source_assignments": deepcopy(first),
			},
			{
				"sequence_no": 2,
				"approval_mode": "Any",
				"approvers": [source("c@example.com")],
				"source_assignments": [source("c@example.com")],
			},
		],
		"decision_digest": "",
	}
	for step in value["steps"]:
		step.update(step_name="Review", threshold_amount="0")
	value["decision_digest"] = fingerprint_json(value)
	return value


def resign(value):
	value["decision_digest"] = ""
	value["decision_digest"] = fingerprint_json(value)
	return value


class TestBudgetHumanWorkflow(unittest.TestCase):
	def test_all_votes_advance_sequentially_and_complete_exactly_once(self):
		policy = decision()
		a = Vote(1, "a@example.com", "Approve")
		b = Vote(1, "b@example.com", "Approve")
		c = Vote(2, "c@example.com", "Approve")
		self.assertEqual(replay_votes(policy, (a,), forbidden=set()).current_step, 1)
		self.assertEqual(replay_votes(policy, (a, b), forbidden=set()).current_step, 2)
		self.assertEqual(replay_votes(policy, (a, b, c), forbidden=set()).status, "Approved")
		self.assertEqual(replay_votes(policy, (b, a, c), forbidden=set()).status, "Approved")

	def test_any_step_and_rejection_do_not_skip_required_later_step(self):
		policy = decision("Any")
		progress = replay_votes(policy, (Vote(1, "a@example.com", "Approve"),), forbidden=set())
		self.assertEqual((progress.status, progress.current_step), ("Pending", 2))
		self.assertEqual(
			replay_votes(policy, (Vote(1, "b@example.com", "Reject"),), forbidden=set()).status, "Rejected"
		)

	def test_duplicates_future_steps_and_terminal_votes_are_rejected(self):
		policy = decision()
		for votes in (
			(Vote(2, "c@example.com", "Approve"),),
			(Vote(1, "a@example.com", "Approve"),) * 2,
			(Vote(1, "a@example.com", "Reject"), Vote(1, "b@example.com", "Approve")),
		):
			with self.subTest(votes=votes), self.assertRaises(ValueError):
				replay_votes(policy, votes, forbidden=set())

	def test_delegation_cannot_evade_preparer_separation(self):
		policy = decision(delegated=True)
		with self.assertRaises(ValueError):
			replay_votes(policy, (Vote(1, "a@example.com", "Approve"),), forbidden={"preparer@example.com"})
		with self.assertRaises(ValueError):
			replay_votes(policy, (Vote(1, "b@example.com", "Approve"),), forbidden={"b@example.com"})

	def test_shared_delegate_retains_all_principals_for_conflict_check(self):
		policy = decision()
		policy["steps"][0]["source_assignments"].append(
			{
				"user": "a@example.com",
				"source_type": "Role",
				"source_value": "Reviewer",
				"delegated_from": "preparer@example.com",
				"delegation": "Grant",
			}
		)
		policy["delegations"] = ["Grant"]
		resign(policy)
		with self.assertRaises(ValueError):
			replay_votes(policy, (Vote(1, "a@example.com", "Approve"),), forbidden={"preparer@example.com"})

	def test_snapshot_tampering_and_incomplete_provenance_are_denied(self):
		value = decision()
		value["amount"] = "2.000000001"
		with self.assertRaises(ValueError):
			parse_decision(value)
		for change in (
			lambda p: p["steps"][0].update(source_assignments=[]),
			lambda p: p["steps"][0]["source_assignments"].append(
				deepcopy(p["steps"][0]["source_assignments"][0])
			),
			lambda p: p["steps"][0]["source_assignments"][0].update(user="foreign@example.com"),
		):
			value = decision()
			change(value)
			resign(value)
			with self.assertRaises(ValueError):
				parse_decision(value)

	def test_explicit_confirmation_is_strict_and_never_accepts_financial_values(self):
		self.assertTrue(
			build_human_confirmation(
				{"confirmation": "Review", "proof": "a" * 43, "acknowledge": True}
			).acknowledge
		)
		for acknowledgement in (False, 1, "true", None):
			with self.subTest(value=acknowledgement), self.assertRaises(ValueError):
				build_human_confirmation(
					{"confirmation": "Review", "proof": "a" * 43, "acknowledge": acknowledgement}
				)
		with self.assertRaises(ValueError):
			build_case_request({"submission": "Snapshot", "expected_revision": 1, "amount": "1"})
		with self.assertRaises(ValueError):
			build_review_intent({"case": "Case", "expected_revision": 1, "action": "Approve", "reason": ""})

	def test_case_request_and_intent_bind_revision_without_client_policy(self):
		self.assertEqual(
			build_case_request({"submission": "Snapshot", "expected_revision": 1}).submission, "Snapshot"
		)
		for key, value in (("expected_revision", True), ("policy_digest", "a" * 64), ("action", "Post")):
			payload = {"case": "Case", "expected_revision": 1, "action": "Approve", "reason": "Reviewed"}
			payload[key] = value
			with self.subTest(key=key), self.assertRaises(ValueError):
				build_review_intent(payload)

	def test_withdrawal_ends_case_without_creating_an_approval(self):
		policy = decision()
		progress = replay_votes(
			policy, (Vote(1, "requester@example.com", "Withdraw"),), forbidden={"requester@example.com"}
		)
		self.assertEqual((progress.status, progress.current_step), ("Withdrawn", 0))
