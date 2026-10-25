from __future__ import annotations

import unittest

from medops.common.organization_mapping import (
	OrganizationMappingContractError,
	build_organization_mapping_resolve,
	build_organization_mapping_upsert,
)


class OrganizationMappingContractTest(unittest.TestCase):
	def test_upsert_normalizes_links_flags_revision_and_remarks(self) -> None:
		command = build_organization_mapping_upsert(
			organization_version="HOSPITAL-V0001",
			organization_unit="HOSPITAL-V0001-OUTPATIENT",
			department="门诊部 - C019",
			cost_center="门诊部 - C019",
			enabled="1",
			expected_revision="0",
			remarks=" 受控映射 ",
		)
		self.assertEqual(command.organization_version, "HOSPITAL-V0001")
		self.assertEqual(command.department, "门诊部 - C019")
		self.assertTrue(command.enabled)
		self.assertEqual(command.expected_revision, 0)
		self.assertEqual(command.remarks, "受控映射")
		payload = command.as_request_payload()
		self.assertNotIn("受控映射", payload.values())
		self.assertEqual(len(str(payload["remarks_digest"])), 64)

	def test_upsert_requires_at_least_one_standard_target(self) -> None:
		with self.assertRaisesRegex(
			OrganizationMappingContractError,
			"department or cost_center is required",
		):
			build_organization_mapping_upsert(
				organization_version="HOSPITAL-V0001",
				organization_unit="HOSPITAL-V0001-OUTPATIENT",
			)

	def test_upsert_rejects_ambiguous_values(self) -> None:
		for kwargs in (
			{"enabled": "true"},
			{"expected_revision": -1},
			{"department": " Department "},
			{"remarks": "x" * 2001},
		):
			with self.subTest(kwargs=kwargs), self.assertRaises(OrganizationMappingContractError):
				build_organization_mapping_upsert(
					**{
						"organization_version": "HOSPITAL-V0001",
						"organization_unit": "HOSPITAL-V0001-OUTPATIENT",
						"department": "门诊部 - C019",
						**kwargs,
					},
				)

	def test_resolve_accepts_direct_organization_unit(self) -> None:
		query = build_organization_mapping_resolve(
			organization_unit="HOSPITAL-V0001-OUTPATIENT",
		)
		self.assertEqual(query.organization_unit, "HOSPITAL-V0001-OUTPATIENT")
		self.assertIsNone(query.hospital)
		self.assertIsNone(query.effective_on)

	def test_resolve_accepts_hospital_code_and_date(self) -> None:
		query = build_organization_mapping_resolve(
			hospital="HOSPITAL",
			unit_code="outpatient",
			effective_on="2026-07-30",
		)
		self.assertEqual(query.hospital, "HOSPITAL")
		self.assertEqual(query.unit_code, "OUTPATIENT")
		self.assertEqual(query.effective_on, "2026-07-30")

	def test_resolve_rejects_mixed_or_incomplete_selectors(self) -> None:
		for kwargs in (
			{},
			{"hospital": "HOSPITAL"},
			{"unit_code": "OUTPATIENT"},
			{
				"organization_unit": "HOSPITAL-V0001-OUTPATIENT",
				"hospital": "HOSPITAL",
				"unit_code": "OUTPATIENT",
			},
			{
				"organization_unit": "HOSPITAL-V0001-OUTPATIENT",
				"effective_on": "2026-07-30",
			},
		):
			with self.subTest(kwargs=kwargs), self.assertRaises(OrganizationMappingContractError):
				build_organization_mapping_resolve(**kwargs)


if __name__ == "__main__":
	unittest.main()
