from __future__ import annotations

import unittest

from medops.common.segregation import (
	SegregationContractError,
	build_actor_fields,
	build_conflicting_roles,
	build_segregation_decision,
	build_segregation_evaluation,
	build_segregation_rule_definition,
	normalize_segregation_action,
)


def _rule(**overrides: object):
	values: dict[str, object] = {
		"code": "PAY-MAKER-CHECKER",
		"display_name": "付款制单与审批分离",
		"target_doctype": "HRP Payment Request",
		"target_action": "approve",
		"company": "I-ONE",
		"hospital": "H001",
		"organization_unit": "FIN",
		"include_descendants": True,
		"actor_fields": ["owner", "reviewed_by"],
		"conflicting_roles": ["HRP Applicant"],
		"enabled": True,
		"valid_from": "2026-08-20",
		"valid_to": None,
		"revision": 3,
		"remarks": "BR-PAY-001",
	}
	return build_segregation_rule_definition(**{**values, **overrides})


def _evaluation(user: str = "maker@example.com"):
	return build_segregation_evaluation(
		user=user,
		action="Approve",
		doctype="HRP Payment Request",
		docname="PAY-2026-00001",
	)


class SegregationContractTests(unittest.TestCase):
	def test_action_and_sources_are_closed_bounded_and_declarative(self) -> None:
		self.assertEqual(normalize_segregation_action("Approve"), "approve")
		self.assertEqual(build_actor_fields('["reviewed_by","owner"]'), ("owner", "reviewed_by"))
		self.assertEqual(build_conflicting_roles('["HRP Applicant"]'), ("HRP Applicant",))
		self.assertEqual(build_actor_fields(("reviewed_by", "owner")), ("owner", "reviewed_by"))
		self.assertEqual(build_conflicting_roles(("HRP Applicant",)), ("HRP Applicant",))
		for invalid in ("execute", "frappe.db.sql", "approve; drop table"):
			with self.subTest(action=invalid), self.assertRaises(SegregationContractError):
				normalize_segregation_action(invalid)
		for invalid_fields in (["owner", "owner"], ["__class__"], ["owner); DROP TABLE"]):
			with self.subTest(fields=invalid_fields), self.assertRaises(SegregationContractError):
				build_actor_fields(invalid_fields)

	def test_rule_requires_a_conflict_source_and_valid_scope(self) -> None:
		with self.assertRaises(SegregationContractError):
			_rule(actor_fields=[], conflicting_roles=[])
		with self.assertRaises(SegregationContractError):
			_rule(organization_unit=None, include_descendants=True)
		with self.assertRaises(SegregationContractError):
			_rule(valid_to="2026-08-19")

	def test_rule_digest_is_deterministic_and_revision_is_external(self) -> None:
		left = _rule(actor_fields=["reviewed_by", "owner"], conflicting_roles=["HRP Applicant"])
		right = _rule(actor_fields=["owner", "reviewed_by"], conflicting_roles=["HRP Applicant"])
		self.assertEqual(left.policy_digest, right.policy_digest)
		self.assertEqual(left.revision, 3)
		self.assertNotEqual(left.policy_digest, _rule(target_action="pay").policy_digest)

	def test_actor_and_role_conflicts_are_redacted_and_fail_closed(self) -> None:
		decision = build_segregation_decision(
			evaluation=_evaluation(),
			evaluated_rules=[
				("PAY-MAKER-CHECKER", _rule(), ("owner",), ("HRP Applicant",)),
			],
		)
		self.assertFalse(decision["allowed"])
		self.assertEqual(decision["conflicts"][0].get("reason"), "ACTOR_AND_ROLE")
		self.assertEqual(decision["conflicts"][0].get("matched_actor_fields"), ["owner"])
		self.assertNotIn("maker@example.com", str(decision))

	def test_missing_rule_denies_while_nonconflicting_rule_allows(self) -> None:
		missing = build_segregation_decision(evaluation=_evaluation(), evaluated_rules=[])
		self.assertFalse(missing["allowed"])
		self.assertEqual(missing["conflicts"][0].get("code"), "NO_APPLICABLE_RULE")
		allowed = build_segregation_decision(
			evaluation=_evaluation(),
			evaluated_rules=[("PAY-MAKER-CHECKER", _rule(), (), ())],
		)
		self.assertTrue(allowed["allowed"])
		self.assertEqual(allowed["conflicts"], [])
		self.assertNotEqual(missing["decision_digest"], allowed["decision_digest"])

	def test_allowed_decision_digest_binds_every_evaluated_rule_version_and_identity(self) -> None:
		primary = _rule(code="PAY-PRIMARY")
		guard = _rule(code="PAY-GUARD")
		base_rules = [
			("PAY-PRIMARY-RULE", primary, (), ()),
			("PAY-GUARD-RULE", guard, (), ()),
		]
		base = build_segregation_decision(evaluation=_evaluation(), evaluated_rules=base_rules)
		renamed = build_segregation_decision(
			evaluation=_evaluation(),
			evaluated_rules=[base_rules[0], ("PAY-GUARD-RULE-V2", guard, (), ())],
		)
		revised = build_segregation_decision(
			evaluation=_evaluation(),
			evaluated_rules=[base_rules[0], ("PAY-GUARD-RULE", _rule(code="PAY-GUARD", revision=4), (), ())],
		)
		policy_changed = build_segregation_decision(
			evaluation=_evaluation(),
			evaluated_rules=[
				base_rules[0],
				("PAY-GUARD-RULE", _rule(code="PAY-GUARD", remarks="BR-PAY-002"), (), ()),
			],
		)

		decisions = (base, renamed, revised, policy_changed)
		self.assertTrue(all(decision["allowed"] for decision in decisions))
		self.assertTrue(all(decision["rules_evaluated"] == 2 for decision in decisions))
		self.assertTrue(all(decision["conflicts"] == [] for decision in decisions))
		self.assertEqual(len({decision["decision_digest"] for decision in decisions}), len(decisions))


if __name__ == "__main__":
	unittest.main()
