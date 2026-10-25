from __future__ import annotations

import unittest

from medops.common.external_code_mapping import (
	GLOBAL_SCOPE_KEY,
	ExternalCodeMappingContractError,
	build_external_code_mapping_resolve,
	build_external_code_mapping_upsert,
	build_internal_code_mapping_resolve,
)


def valid_upsert(**overrides: object) -> dict[str, object]:
	return {
		"master_data_domain": "item",
		"company": "测试法人",
		"hospital": "hospital",
		"external_system": "his",
		"external_code": "001a",
		"external_label": " 一次性耗材 ",
		"internal_name": "ITEM-0001",
		"valid_from": "2026-08-01",
		"remarks": " 外部主数据同步 ",
		**overrides,
	}


class ExternalCodeMappingContractTest(unittest.TestCase):
	def test_upsert_normalizes_scope_codes_flags_and_labels(self) -> None:
		command = build_external_code_mapping_upsert(**valid_upsert(enabled="1"))
		self.assertEqual(command.master_data_domain, "ITEM")
		self.assertEqual(command.external_system, "HIS")
		self.assertEqual(command.external_code, "001a")
		self.assertEqual(command.external_label, "一次性耗材")
		self.assertEqual(command.scope_key, GLOBAL_SCOPE_KEY)
		self.assertTrue(command.enabled)
		self.assertEqual(command.expected_revision, 0)
		self.assertEqual(len(command.source_key), 64)
		self.assertEqual(len(command.target_key), 64)

	def test_source_and_target_keys_are_deterministic_and_directional(self) -> None:
		first = build_external_code_mapping_upsert(**valid_upsert())
		repeat = build_external_code_mapping_upsert(**valid_upsert())
		changed_source = build_external_code_mapping_upsert(**valid_upsert(external_code="001A"))
		changed_target = build_external_code_mapping_upsert(**valid_upsert(internal_name="ITEM-0002"))
		self.assertEqual(first.source_key, repeat.source_key)
		self.assertEqual(first.target_key, repeat.target_key)
		self.assertNotEqual(first.source_key, first.target_key)
		self.assertNotEqual(first.source_key, changed_source.source_key)
		self.assertEqual(first.target_key, changed_source.target_key)
		self.assertEqual(first.source_key, changed_target.source_key)
		self.assertNotEqual(first.target_key, changed_target.target_key)

	def test_organization_scope_changes_both_identity_keys(self) -> None:
		global_mapping = build_external_code_mapping_upsert(**valid_upsert())
		unit_mapping = build_external_code_mapping_upsert(**valid_upsert(organization_unit="UNIT-01"))
		self.assertEqual(unit_mapping.scope_key, "UNIT-01")
		self.assertNotEqual(global_mapping.source_key, unit_mapping.source_key)
		self.assertNotEqual(global_mapping.target_key, unit_mapping.target_key)

	def test_request_payload_redacts_remarks(self) -> None:
		command = build_external_code_mapping_upsert(**valid_upsert())
		payload = command.as_request_payload()
		self.assertNotIn("外部主数据同步", payload.values())
		self.assertEqual(len(str(payload["remarks_digest"])), 64)

	def test_update_requires_mapping_name_and_positive_revision_together(self) -> None:
		updated = build_external_code_mapping_upsert(
			**valid_upsert(mapping_name="mapping-001", expected_revision="2")
		)
		self.assertEqual(updated.mapping_name, "mapping-001")
		self.assertEqual(updated.expected_revision, 2)
		for overrides in (
			{"mapping_name": None, "expected_revision": 1},
			{"mapping_name": "mapping-001", "expected_revision": 0},
		):
			with self.subTest(overrides=overrides), self.assertRaises(ExternalCodeMappingContractError):
				build_external_code_mapping_upsert(**valid_upsert(**overrides))

	def test_upsert_rejects_ambiguous_or_out_of_range_values(self) -> None:
		for overrides in (
			{"enabled": "true"},
			{"master_data_domain": " ITEM"},
			{"external_code": " external "},
			{"external_label": "x" * 141},
			{"valid_to": "2026-07-31"},
			{"remarks": "x" * 2001},
		):
			with self.subTest(overrides=overrides), self.assertRaises(ExternalCodeMappingContractError):
				build_external_code_mapping_upsert(**valid_upsert(**overrides))

	def test_bidirectional_resolvers_require_explicit_effective_date(self) -> None:
		inbound = build_external_code_mapping_resolve(
			master_data_domain="item",
			company="测试法人",
			hospital="hospital",
			external_system="his",
			external_code="001a",
			effective_on="2026-08-03",
		)
		outbound = build_internal_code_mapping_resolve(
			master_data_domain="item",
			company="测试法人",
			hospital="hospital",
			external_system="his",
			internal_name="ITEM-0001",
			effective_on="2026-08-03",
		)
		self.assertEqual(inbound.master_data_domain, "ITEM")
		self.assertEqual(inbound.external_code, "001a")
		self.assertEqual(outbound.internal_name, "ITEM-0001")
		self.assertNotEqual(inbound.source_key, outbound.target_key)
		for builder, selector in (
			(build_external_code_mapping_resolve, {"external_code": "001a"}),
			(build_internal_code_mapping_resolve, {"internal_name": "ITEM-0001"}),
		):
			with self.subTest(builder=builder.__name__), self.assertRaises(ExternalCodeMappingContractError):
				builder(
					master_data_domain="ITEM",
					company="测试法人",
					hospital="HOSPITAL",
					external_system="HIS",
					effective_on=None,
					**selector,
				)


if __name__ == "__main__":
	unittest.main()
