from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from medops.common.fixture_policy import (
	FIXTURE_DIRECTORY,
	POLICY_PATH,
	FixturePolicyError,
	canonicalize_fixture_repository,
	get_frappe_fixture_hooks,
	inspect_fixture_repository,
	load_fixture_policy,
	normalize_fixture_payload,
)


class FixturePolicyTest(unittest.TestCase):
	def setUp(self) -> None:
		self.policy = load_fixture_policy()

	def test_current_policy_is_allowlisted_ordered_and_redacted(self) -> None:
		self.assertEqual(
			[rule.doctype for rule in self.policy.rules],
			["Custom Field", "Property Setter", "Custom DocPerm"],
		)
		self.assertEqual(
			self.policy.expected_filenames,
			("1_custom_field.json", "2_property_setter.json", "3_custom_docperm.json"),
		)
		self.assertEqual(len(self.policy.rules[0].ownership_values), 36)
		self.assertEqual(len(self.policy.rules[2].ownership_values), 5)
		public = self.policy.as_public_dict()
		self.assertNotIn("ownership_values", json.dumps(public))
		self.assertEqual(get_frappe_fixture_hooks()[0]["dt"], "Custom Field")

	def test_committed_fixture_repository_is_canonical(self) -> None:
		report = inspect_fixture_repository(self.policy)
		self.assertEqual(report.files, 3)
		self.assertEqual(report.records, 0)
		self.assertEqual(len(report.sha256), 64)

	def test_policy_rejects_unknown_keys_and_out_of_order_dependencies(self) -> None:
		payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "policy.json"
			payload["unexpected"] = True
			path.write_text(json.dumps(payload), encoding="utf-8")
			with self.assertRaisesRegex(FixturePolicyError, "keys mismatch"):
				load_fixture_policy(path)
			del payload["unexpected"]
			payload["rules"][0]["depends_on"] = ["Property Setter"]
			path.write_text(json.dumps(payload), encoding="utf-8")
			with self.assertRaisesRegex(FixturePolicyError, "dependency must appear earlier"):
				load_fixture_policy(path)

	def test_record_must_be_owned_by_the_declared_module(self) -> None:
		rule = self.policy.get_rule("Custom Field")
		payload = [
			{
				"doctype": "Custom Field",
				"name": "Company-medops_probe",
				"module": "Accounts",
			}
		]
		with self.assertRaisesRegex(FixturePolicyError, "is not owned"):
			normalize_fixture_payload(self.policy, rule, payload)

	def test_sensitive_fields_and_values_are_rejected(self) -> None:
		rule = self.policy.get_rule("Custom Field")
		base = {
			"doctype": "Custom Field",
			"name": "Company-medops_probe",
			"module": "HRP Foundation",
		}
		with self.assertRaisesRegex(FixturePolicyError, "sensitive field"):
			normalize_fixture_payload(self.policy, rule, [{**base, "api_secret": "not-allowed"}])
		with self.assertRaisesRegex(FixturePolicyError, "sensitive value"):
			normalize_fixture_payload(
				self.policy,
				rule,
				[{**base, "description": "-----BEGIN PRIVATE KEY-----"}],
			)

	def test_canonicalization_strips_volatile_fields_sorts_and_is_idempotent(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			fixture_directory = Path(directory)
			for filename in self.policy.expected_filenames:
				(fixture_directory / filename).write_text("[]\n", encoding="utf-8")
			payload = [
				{
					"doctype": "Custom Field",
					"name": "Company-medops_z",
					"module": "HRP Foundation",
					"modified": "2026-07-29 00:00:00",
				},
				{
					"module": "HRP Foundation",
					"name": "Company-medops_a",
					"doctype": "Custom Field",
					"owner": "Administrator",
				},
			]
			path = fixture_directory / "1_custom_field.json"
			path.write_text(json.dumps(payload), encoding="utf-8")
			first = canonicalize_fixture_repository(self.policy, fixture_directory)
			first_text = path.read_text(encoding="utf-8")
			second = canonicalize_fixture_repository(self.policy, fixture_directory)
			self.assertEqual(first.sha256, second.sha256)
			self.assertEqual(first_text, path.read_text(encoding="utf-8"))
			self.assertNotIn("modified", first_text)
			self.assertNotIn("owner", first_text)
			self.assertLess(first_text.index("medops_a"), first_text.index("medops_z"))

	def test_extra_fixture_file_is_rejected(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			fixture_directory = Path(directory)
			for source in FIXTURE_DIRECTORY.iterdir():
				(fixture_directory / source.name).write_bytes(source.read_bytes())
			(fixture_directory / "user.json").write_text("[]\n", encoding="utf-8")
			with self.assertRaisesRegex(FixturePolicyError, "extra=.*user.json"):
				inspect_fixture_repository(self.policy, fixture_directory)


if __name__ == "__main__":
	unittest.main()
