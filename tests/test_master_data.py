from __future__ import annotations

import json
import unittest

from medops.common.master_data import (
	MASTER_DATA_TARGET_POLICIES,
	MAX_CHANGE_ITEMS,
	MasterDataContractError,
	build_master_data_domain_upsert,
	build_master_data_request_review,
	build_master_data_request_submit,
	build_master_data_request_upsert,
	get_target_policy,
	normalize_policy_value,
	normalize_proposed_changes,
)


def valid_request(**overrides: object) -> dict[str, object]:
	return {
		"master_data_domain": "ITEM",
		"company": "测试法人",
		"hospital": "HOSPITAL",
		"organization_unit": "HOSPITAL-V0001-OUTPATIENT",
		"operation": "Create",
		"subject": "新增诊疗物料",
		"effective_on": "2026-07-30",
		"changes": [
			{"field_name": "item_name", "proposed_value": "一次性耗材"},
			{"field_name": "item_group", "proposed_value": "医疗耗材"},
			{"field_name": "stock_uom", "proposed_value": "个"},
		],
		**overrides,
	}


class MasterDataContractTest(unittest.TestCase):
	def test_static_registry_contains_only_supported_standard_targets(self) -> None:
		self.assertEqual(
			set(MASTER_DATA_TARGET_POLICIES),
			{"Department", "Cost Center", "Item", "Supplier", "Warehouse"},
		)
		for target, policy in MASTER_DATA_TARGET_POLICIES.items():
			with self.subTest(target=target):
				self.assertEqual(policy.target_doctype, target)
				self.assertEqual(len(policy.digest), 64)
				self.assertTrue(policy.fields)
				self.assertEqual(len(policy.fields), len(policy.fields_by_name))

	def test_domain_upsert_normalizes_code_boolean_revision_and_remarks(self) -> None:
		command = build_master_data_domain_upsert(
			code="item",
			display_name=" 物料主数据 ",
			target_doctype="Item",
			enabled="1",
			expected_revision="0",
			remarks=" 受控领域 ",
		)
		self.assertEqual(command.code, "ITEM")
		self.assertEqual(command.display_name, "物料主数据")
		self.assertTrue(command.enabled)
		self.assertEqual(command.expected_revision, 0)
		self.assertEqual(command.remarks, "受控领域")

	def test_domain_rejects_unknown_target_and_ambiguous_values(self) -> None:
		for target in ("User", "GL Entry", "", None):
			with self.subTest(target=target), self.assertRaises(MasterDataContractError):
				get_target_policy(target)
		for kwargs in (
			{"enabled": "true"},
			{"expected_revision": -1},
			{"code": " ITEM"},
			{"display_name": "x" * 501},
		):
			with self.subTest(kwargs=kwargs), self.assertRaises(MasterDataContractError):
				build_master_data_domain_upsert(
					**{
						"code": "ITEM",
						"display_name": "物料主数据",
						"target_doctype": "Item",
						"expected_revision": 0,
						**kwargs,
					},
				)

	def test_change_payload_is_strict_bounded_and_unique(self) -> None:
		changes = normalize_proposed_changes(
			json.dumps(
				[
					{"field_name": "item_name", "proposed_value": " 物料甲 "},
					{"field_name": "disabled", "proposed_value": True, "reason": " 停用 "},
				],
				ensure_ascii=False,
			)
		)
		self.assertEqual(changes[0].proposed_value, "物料甲")
		self.assertEqual(changes[1].proposed_value, "1")
		self.assertEqual(changes[1].reason, "停用")
		for payload in (
			[],
			[{"field_name": "item_name", "proposed_value": "甲", "extra": 1}],
			[
				{"field_name": "item_name", "proposed_value": "甲"},
				{"field_name": "item_name", "proposed_value": "乙"},
			],
			[{"field_name": "api_secret", "proposed_value": "secret"}],
			[{"field_name": "password_hash", "proposed_value": "secret"}],
			[{"field_name": "Item Name", "proposed_value": "甲"}],
			[{"field_name": "item_name", "proposed_value": {"unsafe": True}}],
			[
				{"field_name": f"field_{index}", "proposed_value": str(index)}
				for index in range(MAX_CHANGE_ITEMS + 1)
			],
		):
			with self.subTest(payload=payload), self.assertRaises(MasterDataContractError):
				normalize_proposed_changes(payload)

	def test_policy_value_types_are_deterministic(self) -> None:
		item = get_target_policy("Item").fields_by_name
		self.assertEqual(normalize_policy_value(item["disabled"], "1", allow_empty=False), "1")
		self.assertEqual(
			normalize_policy_value(item["item_name"], "耗材", allow_empty=False),
			"耗材",
		)
		for value in ("true", 2, None):
			with self.subTest(value=value), self.assertRaises(MasterDataContractError):
				normalize_policy_value(item["disabled"], value, allow_empty=False)
		supplier_type = get_target_policy("Supplier").fields_by_name["supplier_type"]
		with self.assertRaises(MasterDataContractError):
			normalize_policy_value(supplier_type, "Government", allow_empty=False)

	def test_create_request_has_explicit_create_revision_contract(self) -> None:
		command = build_master_data_request_upsert(**valid_request())
		self.assertIsNone(command.request_name)
		self.assertIsNone(command.target_name)
		self.assertEqual(command.expected_revision, 0)
		self.assertEqual(command.operation, "Create")
		self.assertEqual(command.effective_on, "2026-07-30")
		self.assertEqual(len(command.changes), 3)

	def test_update_and_disable_require_target_and_existing_revision_shape(self) -> None:
		update = build_master_data_request_upsert(
			**valid_request(
				request_name="MDR-2026-00001",
				expected_revision="2",
				operation="Update",
				target_name="ITEM-0001",
				changes=[{"field_name": "item_name", "proposed_value": "新名称"}],
			)
		)
		self.assertEqual(update.target_name, "ITEM-0001")
		self.assertEqual(update.expected_revision, 2)
		for overrides in (
			{"operation": "Update", "target_name": None},
			{"operation": "Create", "target_name": "ITEM-0001"},
			{"request_name": "MDR-2026-00001", "expected_revision": 0},
			{"request_name": None, "expected_revision": 1},
			{"operation": "Delete"},
		):
			with self.subTest(overrides=overrides), self.assertRaises(MasterDataContractError):
				build_master_data_request_upsert(**valid_request(**overrides))

	def test_submit_and_review_require_positive_revision_and_rejection_reason(self) -> None:
		submit = build_master_data_request_submit(
			request_name="MDR-2026-00001",
			expected_revision="1",
		)
		self.assertEqual(submit.expected_revision, 1)
		approve = build_master_data_request_review(
			request_name="MDR-2026-00001",
			expected_revision=2,
			decision="Approve",
		)
		self.assertEqual(approve.decision, "Approve")
		reject = build_master_data_request_review(
			request_name="MDR-2026-00001",
			expected_revision=2,
			decision="Reject",
			reason=" 信息不足 ",
		)
		self.assertEqual(reject.reason, "信息不足")
		for kwargs in (
			{"decision": "Reject", "reason": None},
			{"decision": "Execute", "reason": None},
			{"decision": "Approve", "expected_revision": 0},
		):
			with self.subTest(kwargs=kwargs), self.assertRaises(MasterDataContractError):
				build_master_data_request_review(
					**{
						"request_name": "MDR-2026-00001",
						"expected_revision": 2,
						**kwargs,
					},
				)


if __name__ == "__main__":
	unittest.main()
