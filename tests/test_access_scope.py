from __future__ import annotations

import unittest

from medops.common.access_scope import (
	ACCESS_ACTIONS,
	AccessScopeContractError,
	ScopeGrant,
	build_access_scope_definition,
	build_access_scope_member,
	build_scope_decision,
	filter_group_for,
	normalize_action,
)


def member(**overrides: object):
	values = {
		"user": "user@example.com",
		"target_doctype": "HRP Organization Unit",
		"allow_read": True,
	}
	values.update(overrides)
	return build_access_scope_member(**values)


class AccessScopeContractTests(unittest.TestCase):
	def test_actions_are_normalized_and_allowlisted(self) -> None:
		self.assertEqual(tuple(normalize_action(action.upper()) for action in ACCESS_ACTIONS), ACCESS_ACTIONS)
		for action in ("delete", "execute", "read;drop", ""):
			with self.subTest(action=action), self.assertRaises(AccessScopeContractError):
				normalize_action(action)

	def test_member_requires_one_action_when_enabled(self) -> None:
		with self.assertRaises(AccessScopeContractError):
			member(allow_read=False)
		disabled = member(allow_read=False, enabled=False)
		self.assertFalse(disabled.enabled)

	def test_scope_levels_follow_organization_specificity(self) -> None:
		company = build_access_scope_definition(
			code="company",
			display_name="法人范围",
			company="I-ONE",
			members=[member()],
		)
		hospital = build_access_scope_definition(
			code="hospital",
			display_name="医院范围",
			company="I-ONE",
			hospital="h001",
			members=[member()],
		)
		unit = build_access_scope_definition(
			code="department",
			display_name="科室范围",
			company="I-ONE",
			hospital="h001",
			organization_unit="H001-FIN",
			include_descendants=True,
			members=[member()],
		)
		self.assertEqual(
			(company.scope_level, hospital.scope_level, unit.scope_level),
			("Company", "Hospital", "Organization Unit"),
		)

	def test_descendants_require_an_organization_unit(self) -> None:
		with self.assertRaises(AccessScopeContractError):
			build_access_scope_definition(
				code="invalid",
				display_name="无效范围",
				company="I-ONE",
				hospital="H001",
				include_descendants=True,
				members=[member()],
			)

	def test_member_identity_is_unique_and_digest_is_order_independent(self) -> None:
		first = member(user="a@example.com")
		second = member(user="b@example.com", allow_write=True)
		left = build_access_scope_definition(
			code="team",
			display_name="团队范围",
			company="I-ONE",
			members=[first, second],
		)
		right = build_access_scope_definition(
			code="team",
			display_name="团队范围",
			company="I-ONE",
			members=[second, first],
		)
		self.assertEqual(left.policy_digest, right.policy_digest)
		with self.assertRaises(AccessScopeContractError):
			build_access_scope_definition(
				code="duplicate",
				display_name="重复授权",
				company="I-ONE",
				members=[first, first],
			)

	def test_filter_groups_use_only_reviewed_dimension_fields(self) -> None:
		grant = ScopeGrant(
			code="FIN",
			company="I-ONE",
			hospital="H001",
			organization_unit="FIN",
			include_descendants=True,
			organization_units=("FIN", "AP"),
		)
		group = filter_group_for(
			grant,
			dimension_fields={
				"company": "company",
				"hospital": "hospital",
				"organization_unit": "department_unit",
			},
		)
		self.assertEqual(
			group,
			{
				"operator": "AND",
				"conditions": [
					{"field": "company", "operator": "=", "value": "I-ONE"},
					{"field": "hospital", "operator": "=", "value": "H001"},
					{"field": "department_unit", "operator": "in", "value": ["FIN", "AP"]},
				],
			},
		)
		self.assertIsNone(filter_group_for(grant, dimension_fields={"company": "company"}))

	def test_decision_fails_closed_without_scope_or_base_permission(self) -> None:
		base = {
			"user": "user@example.com",
			"doctype": "Purchase Order",
			"action": "read",
			"dimension_fields": {"company": "company"},
		}
		without_scope = build_scope_decision(**base, grants=(), base_permission=True)
		without_role = build_scope_decision(
			**base,
			grants=(ScopeGrant("C", "I-ONE", None, None, False, ()),),
			base_permission=False,
		)
		self.assertFalse(without_scope["allowed"])
		filters = without_scope["filters"]
		self.assertIsInstance(filters, dict)
		self.assertTrue(filters["fail_closed"])
		self.assertFalse(without_role["allowed"])

	def test_admin_decision_is_unrestricted_but_still_requires_base_permission(self) -> None:
		decision = build_scope_decision(
			user="Administrator",
			doctype="Company",
			action="read",
			dimension_fields={"company": "name"},
			grants=(),
			base_permission=True,
			unrestricted=True,
		)
		self.assertTrue(decision["allowed"])
		self.assertTrue(decision["unrestricted"])
		filters = decision["filters"]
		self.assertIsInstance(filters, dict)
		self.assertEqual(filters["groups"], [])


if __name__ == "__main__":
	unittest.main()
