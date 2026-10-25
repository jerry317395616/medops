from __future__ import annotations

import json
import unittest

from medops.common.organization import (
	MAX_HIERARCHY_NODES,
	OrganizationContractError,
	build_hierarchy_replace,
	build_hospital_upsert,
	build_organization_version_create,
	build_organization_version_publish,
	hierarchy_digest,
	normalize_code,
	normalize_hierarchy_nodes,
)


def valid_nodes() -> list[dict[str, object]]:
	return [
		{
			"code": "HOSPITAL",
			"display_name": "测试医院",
			"unit_type": "HOSPITAL",
			"parent_code": None,
			"is_group": 1,
			"sequence": 1,
		},
		{
			"code": "OUTPATIENT",
			"display_name": "门诊部",
			"unit_type": "CLINICAL_DEPARTMENT",
			"parent_code": "HOSPITAL",
			"is_group": 1,
			"sequence": 2,
		},
		{
			"code": "CARDIOLOGY",
			"display_name": "心内科",
			"unit_type": "CLINICAL_DEPARTMENT",
			"parent_code": "OUTPATIENT",
			"is_group": 0,
			"sequence": 1,
		},
	]


class OrganizationContractTest(unittest.TestCase):
	def test_codes_are_canonical_and_bounded(self) -> None:
		self.assertEqual(normalize_code("hospital", label="code"), "HOSPITAL")
		self.assertEqual(normalize_code("A_01-2", label="code"), "A_01-2")
		for value in (" hospital", "hospital ", "1HOSPITAL", "A B", "A/B", "", 123):
			with self.subTest(value=value), self.assertRaises(OrganizationContractError):
				normalize_code(value, label="code")

	def test_hospital_upsert_has_explicit_create_and_update_revisions(self) -> None:
		create = build_hospital_upsert(
			code="hospital",
			company="测试法人",
			display_name="测试医院",
			enabled="1",
			valid_from="2026-01-01",
			valid_to="2026-12-31",
			remarks=" 受控备注 ",
			expected_revision="0",
		)
		self.assertEqual(create.code, "HOSPITAL")
		self.assertEqual(create.expected_revision, 0)
		self.assertEqual(create.remarks, "受控备注")
		update = build_hospital_upsert(
			code="HOSPITAL",
			company="测试法人",
			display_name="测试医院",
			expected_revision=2,
		)
		self.assertEqual(update.expected_revision, 2)

	def test_hospital_rejects_invalid_date_range_and_ambiguous_boolean(self) -> None:
		with self.assertRaises(OrganizationContractError):
			build_hospital_upsert(
				code="HOSPITAL",
				company="测试法人",
				display_name="测试医院",
				valid_from="2026-02-01",
				valid_to="2026-01-01",
			)
		with self.assertRaises(OrganizationContractError):
			build_hospital_upsert(
				code="HOSPITAL",
				company="测试法人",
				display_name="测试医院",
				enabled="true",
			)

	def test_version_create_and_publish_are_strict(self) -> None:
		create = build_organization_version_create(
			hospital="hospital",
			effective_from="2026-01-01",
			version_label="2026年组织架构",
			remarks=None,
		)
		self.assertEqual(create.hospital, "HOSPITAL")
		self.assertEqual(create.effective_from, "2026-01-01")
		publish = build_organization_version_publish(
			organization_version="HOSPITAL-V0001",
			expected_revision="2",
		)
		self.assertEqual(publish.expected_revision, 2)
		with self.assertRaises(OrganizationContractError):
			build_organization_version_publish(
				organization_version="HOSPITAL-V0001",
				expected_revision=0,
			)

	def test_hierarchy_is_topologically_and_sibling_ordered(self) -> None:
		payload = valid_nodes()
		payload.reverse()
		nodes = normalize_hierarchy_nodes(json.dumps(payload, ensure_ascii=False))
		self.assertEqual([node.code for node in nodes], ["HOSPITAL", "OUTPATIENT", "CARDIOLOGY"])
		self.assertTrue(nodes[0].is_group)
		self.assertFalse(nodes[-1].is_group)

	def test_hierarchy_requires_one_hospital_group_root(self) -> None:
		payload = valid_nodes()
		payload[0]["unit_type"] = "CAMPUS"
		with self.assertRaises(OrganizationContractError):
			normalize_hierarchy_nodes(payload)
		payload = valid_nodes()
		payload.append(
			{
				"code": "OTHER_ROOT",
				"display_name": "第二根节点",
				"unit_type": "HOSPITAL",
				"is_group": 1,
			}
		)
		with self.assertRaises(OrganizationContractError):
			normalize_hierarchy_nodes(payload)

	def test_hierarchy_rejects_duplicates_missing_parents_and_leaf_children(self) -> None:
		payload = valid_nodes()
		payload[2]["code"] = "OUTPATIENT"
		with self.assertRaises(OrganizationContractError):
			normalize_hierarchy_nodes(payload)
		payload = valid_nodes()
		payload[2]["parent_code"] = "MISSING"
		with self.assertRaises(OrganizationContractError):
			normalize_hierarchy_nodes(payload)
		payload = valid_nodes()
		payload[1]["is_group"] = 0
		with self.assertRaises(OrganizationContractError):
			normalize_hierarchy_nodes(payload)

	def test_hierarchy_rejects_cycles_unknown_fields_and_inactive_snapshot_dates(self) -> None:
		payload = valid_nodes()
		payload[1]["parent_code"] = "CARDIOLOGY"
		payload[1]["is_group"] = 1
		payload[2]["is_group"] = 1
		with self.assertRaises(OrganizationContractError):
			normalize_hierarchy_nodes(payload)
		payload = valid_nodes()
		payload[0]["unsupported"] = "value"
		with self.assertRaises(OrganizationContractError):
			normalize_hierarchy_nodes(payload)
		payload = valid_nodes()
		payload[2]["valid_from"] = "2026-02-01"
		payload[2]["valid_to"] = "2026-01-01"
		with self.assertRaises(OrganizationContractError):
			normalize_hierarchy_nodes(payload)

	def test_hierarchy_digest_is_independent_of_request_order(self) -> None:
		first = normalize_hierarchy_nodes(valid_nodes())
		reversed_payload = list(reversed(valid_nodes()))
		second = normalize_hierarchy_nodes(reversed_payload)
		self.assertEqual(hierarchy_digest(first), hierarchy_digest(second))
		command = build_hierarchy_replace(
			organization_version="HOSPITAL-V0001",
			expected_revision=1,
			nodes=valid_nodes(),
		)
		self.assertEqual(command.digest, hierarchy_digest(first))
		self.assertEqual(command.as_request_payload()["node_count"], 3)
		self.assertNotIn("nodes", command.as_request_payload())

	def test_hierarchy_node_count_is_bounded(self) -> None:
		payload = valid_nodes()
		payload.extend(
			{
				"code": f"NODE_{index}",
				"display_name": f"组织{index}",
				"unit_type": "OTHER",
				"parent_code": "HOSPITAL",
				"is_group": 0,
			}
			for index in range(MAX_HIERARCHY_NODES)
		)
		with self.assertRaises(OrganizationContractError):
			normalize_hierarchy_nodes(payload)

	def test_hierarchy_payload_size_is_bounded_for_parsed_arrays(self) -> None:
		nodes = [
			{
				"code": f"N{index}",
				"display_name": f"节点{index}",
				"unit_type": "OTHER",
				"parent_code": None,
				"is_group": True,
				"remarks": "院" * 500,
			}
			for index in range(MAX_HIERARCHY_NODES)
		]
		with self.assertRaisesRegex(OrganizationContractError, "payload is too large"):
			normalize_hierarchy_nodes(nodes)


if __name__ == "__main__":
	unittest.main()
