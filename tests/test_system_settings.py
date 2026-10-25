from __future__ import annotations

import unittest

from medops.common.system_settings import (
	LOCKED_RELEASE_CHANNEL,
	MAX_REFERENCE_LENGTH,
	MAX_REMARKS_LENGTH,
	SystemSettingsContractError,
	build_system_settings_state,
	build_system_settings_update,
	changed_mutable_fields,
	normalize_boolean,
	normalize_optional_reference,
	normalize_optional_remarks,
	normalize_positive_integer,
	normalize_timeout,
)


class SystemSettingsContractTest(unittest.TestCase):
	def test_boolean_contract_is_explicit(self) -> None:
		for value, expected in (
			(True, True),
			(False, False),
			(1, True),
			(0, False),
			("1", True),
			("0", False),
		):
			with self.subTest(value=value):
				self.assertIs(normalize_boolean(value, label="enabled"), expected)
		for value in ("true", "false", 2, -1, None, 1.0):
			with self.subTest(value=value), self.assertRaises(SystemSettingsContractError):
				normalize_boolean(value, label="enabled")

	def test_positive_integer_contract_rejects_boolean_and_noncanonical_text(self) -> None:
		self.assertEqual(normalize_positive_integer(1, label="version"), 1)
		self.assertEqual(normalize_positive_integer("42", label="version"), 42)
		for value in (True, False, 0, "0", "-1", " 1", "1 ", "1.0", 1.0, None):
			with self.subTest(value=value), self.assertRaises(SystemSettingsContractError):
				normalize_positive_integer(value, label="version")

	def test_reference_contract_rejects_ambiguous_or_controlled_text(self) -> None:
		self.assertIsNone(normalize_optional_reference(None, label="company"))
		self.assertIsNone(normalize_optional_reference("", label="company"))
		self.assertEqual(normalize_optional_reference("医院甲", label="hospital"), "医院甲")
		for value in (
			" company",
			"company ",
			"company\nother",
			"a" * (MAX_REFERENCE_LENGTH + 1),
			123,
		):
			with self.subTest(value=value), self.assertRaises(SystemSettingsContractError):
				normalize_optional_reference(value, label="company")

	def test_remarks_are_normalized_and_bounded(self) -> None:
		self.assertIsNone(normalize_optional_remarks(" \r\n "))
		self.assertEqual(normalize_optional_remarks(" 第一行\r\n第二行 "), "第一行\n第二行")
		self.assertEqual(normalize_optional_remarks("列1\t列2"), "列1\t列2")
		for value in ("a" * (MAX_REMARKS_LENGTH + 1), "unsafe\u0000value", 123):
			with self.subTest(value=value), self.assertRaises(SystemSettingsContractError):
				normalize_optional_remarks(value)

	def test_timeout_is_bounded(self) -> None:
		self.assertEqual(normalize_timeout(5), 5)
		self.assertEqual(normalize_timeout("300"), 300)
		for value in (4, 301, "4", "301", True, "30.0"):
			with self.subTest(value=value), self.assertRaises(SystemSettingsContractError):
				normalize_timeout(value)

	def test_update_requires_company_for_hospital(self) -> None:
		command = build_system_settings_update(
			enabled="1",
			default_company="示例公司",
			default_hospital="示例医院",
			integration_timeout_seconds="60",
			remarks="受控说明",
			expected_version="3",
		)
		self.assertTrue(command.enabled)
		self.assertEqual(command.expected_version, 3)
		self.assertEqual(command.integration_timeout_seconds, 60)
		with self.assertRaises(SystemSettingsContractError):
			build_system_settings_update(
				enabled=True,
				default_company=None,
				default_hospital="示例医院",
				integration_timeout_seconds=30,
				remarks=None,
				expected_version=1,
			)

	def test_fixed_security_policy_cannot_be_weakened(self) -> None:
		baseline = {
			"enabled": True,
			"default_company": None,
			"default_hospital": None,
			"integration_timeout_seconds": 30,
			"remarks": None,
			"configuration_version": 1,
			"release_channel": LOCKED_RELEASE_CHANNEL,
			"strict_data_scope": True,
			"require_human_confirmation_for_ai": True,
		}
		state = build_system_settings_state(**baseline)
		self.assertTrue(state.strict_data_scope)
		self.assertTrue(state.require_human_confirmation_for_ai)
		for override in (
			{"release_channel": "develop"},
			{"strict_data_scope": False},
			{"require_human_confirmation_for_ai": False},
		):
			with self.subTest(override=override), self.assertRaises(SystemSettingsContractError):
				build_system_settings_state(**{**baseline, **override})

	def test_state_and_change_set_are_deterministic_without_arbitrary_config(self) -> None:
		state = build_system_settings_state(
			enabled=True,
			default_company="示例公司",
			default_hospital="示例医院",
			integration_timeout_seconds=30,
			remarks=None,
			configuration_version=7,
			release_channel=LOCKED_RELEASE_CHANNEL,
			strict_data_scope=True,
			require_human_confirmation_for_ai=True,
		)
		update = build_system_settings_update(
			enabled=False,
			default_company="示例公司",
			default_hospital=None,
			integration_timeout_seconds=60,
			remarks="更新",
			expected_version=7,
		)
		self.assertEqual(
			changed_mutable_fields(state, update),
			(
				"enabled",
				"default_hospital",
				"integration_timeout_seconds",
				"remarks",
			),
		)
		payload = state.as_public_dict()
		self.assertEqual(payload["schema_version"], 1)
		self.assertEqual(payload["doctype"], "HRP System Settings")
		self.assertEqual(payload["configuration_version"], 7)
		self.assertNotIn("configuration_json", payload)


if __name__ == "__main__":
	unittest.main()
