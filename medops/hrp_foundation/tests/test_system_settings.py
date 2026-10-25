from __future__ import annotations

import json
from typing import cast
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.domain_service import idempotency_record_name
from medops.common.error_catalog import IoneApplicationError
from medops.common.system_settings import (
	LOCKED_RELEASE_CHANNEL,
	SystemSettingsUpdate,
	build_system_settings_update,
)
from medops.hrp_foundation.services.system_settings import (
	UpdateSystemSettingsService,
	get_system_settings,
	update_system_settings,
)
from medops.setup.settings import ensure_system_settings

GET_SETTINGS_METHOD = "medops.api.v1.settings.get_system_settings"
UPDATE_SETTINGS_METHOD = "medops.api.v1.settings.update_system_settings"


def current_state() -> dict[str, object]:
	return get_system_settings()


def command_from_state(
	state: dict[str, object],
	**overrides: object,
) -> SystemSettingsUpdate:
	return build_system_settings_update(
		enabled=overrides.get("enabled", state["enabled"]),
		default_company=overrides.get("default_company", state["default_company"]),
		default_hospital=overrides.get("default_hospital", state["default_hospital"]),
		integration_timeout_seconds=overrides.get(
			"integration_timeout_seconds",
			state["integration_timeout_seconds"],
		),
		remarks=overrides.get("remarks", state["remarks"]),
		expected_version=overrides.get(
			"expected_version",
			state["configuration_version"],
		),
	)


def apply_settings(
	command: SystemSettingsUpdate,
	deduplication_id: object | None,
	correlation_id: object | None,
) -> dict[str, object]:
	return update_system_settings(
		command,
		idempotency_key=deduplication_id,
		correlation_id=correlation_id,
	)


class TestSystemSettings(IntegrationTestCase):
	def test_metadata_is_explicit_chinese_and_admin_only(self) -> None:
		meta = frappe.get_meta("HRP System Settings")
		self.assertTrue(meta.issingle)
		self.assertTrue(meta.track_changes)
		fields = {field.fieldname: field for field in meta.fields}
		self.assertNotIn("configuration_json", fields)
		self.assertEqual(fields["enabled"].label, "启用")
		self.assertEqual(fields["default_company"].options, "Company")
		self.assertEqual(fields["default_hospital"].fieldtype, "Link")
		self.assertEqual(fields["default_hospital"].options, "HRP Hospital")
		for fieldname in (
			"release_channel",
			"configuration_version",
			"strict_data_scope",
			"require_human_confirmation_for_ai",
		):
			self.assertTrue(fields[fieldname].read_only)
		roles = {permission.role for permission in meta.permissions}
		self.assertSetEqual(roles, {"System Manager", "HRP System Manager"})
		self.assertFalse(any(permission.delete for permission in meta.permissions))

	def test_migration_repair_is_idempotent_and_keeps_locked_policy(self) -> None:
		first = ensure_system_settings()
		second = ensure_system_settings()
		state = current_state()
		self.assertFalse(first["changed"])
		self.assertFalse(second["changed"])
		self.assertEqual(
			frappe.db.sql(
				"""
				SELECT COUNT(*)
				FROM `tabSingles`
				WHERE doctype = %s AND field = %s
				""",
				("HRP System Settings", "configuration_version"),
			)[0][0],
			1,
		)
		self.assertEqual(state["release_channel"], LOCKED_RELEASE_CHANNEL)
		self.assertTrue(state["strict_data_scope"])
		self.assertTrue(state["require_human_confirmation_for_ai"])

	def test_read_and_write_require_system_settings_admin_role(self) -> None:
		before = frappe.db.count("HRP Service Idempotency")
		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			self.assertRaises(IoneApplicationError) as read_error,
		):
			get_system_settings()
		self.assertEqual(read_error.exception.code, "IONE-CORE-0002")

		state = current_state()
		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			self.assertRaises(IoneApplicationError) as write_error,
		):
			apply_settings(
				command_from_state(state),
				"COD-017-permission-0001",
				"COD-017-permission",
			)
		self.assertEqual(write_error.exception.code, "IONE-CORE-0002")
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)

	def test_invalid_company_is_rejected_before_idempotency_reservation(self) -> None:
		state = current_state()
		before = frappe.db.count("HRP Service Idempotency")
		with self.assertRaises(IoneApplicationError) as raised:
			apply_settings(
				command_from_state(
					state,
					default_company="COD-017-company-that-does-not-exist",
				),
				"COD-017-invalid-company",
				"COD-017-invalid-company",
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0004")
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)
		self.assertEqual(current_state(), state)

	def test_update_is_versioned_idempotent_and_value_redacted(self) -> None:
		state = current_state()
		timeout = 31 if state["integration_timeout_seconds"] != 31 else 32
		redaction_sentinel = "COD-017-redaction-sentinel"
		deduplication_id = "COD-017-idempotent-0001"
		command = command_from_state(
			state,
			integration_timeout_seconds=timeout,
			remarks=redaction_sentinel,
		)
		with patch("medops.services.audit_context.frappe.logger") as logger:
			first = apply_settings(
				command,
				deduplication_id,
				"COD-017-idempotent-first",
			)
			second = apply_settings(
				command,
				deduplication_id,
				"COD-017-idempotent-second",
			)

		self.assertTrue(first["changed"])
		self.assertFalse(first["idempotency_replayed"])
		self.assertEqual(
			first["configuration_version"],
			cast(int, state["configuration_version"]) + 1,
		)
		self.assertEqual(
			first["changed_fields"],
			["integration_timeout_seconds", "remarks"],
		)
		self.assertTrue(second["idempotency_replayed"])
		self.assertEqual(second["configuration_version"], first["configuration_version"])
		self.assertNotEqual(second["request_id"], first["request_id"])

		record = frappe.get_doc(
			"HRP Service Idempotency",
			idempotency_record_name(
				UpdateSystemSettingsService.definition.name,
				deduplication_id,
			),
		)
		self.assertEqual(record.status, "Completed")
		self.assertNotIn(redaction_sentinel, record.as_json())
		audit_payload = json.dumps(
			[
				call.args[0]
				for level in (logger.return_value.info, logger.return_value.warning)
				for call in level.call_args_list
			],
			ensure_ascii=False,
		)
		self.assertNotIn(redaction_sentinel, audit_payload)
		self.assertIn("integration_timeout_seconds,remarks", audit_payload)

	def test_stale_version_conflict_rolls_back_reservation_and_mutation(self) -> None:
		state = current_state()
		before = frappe.db.count("HRP Service Idempotency")
		with self.assertRaises(IoneApplicationError) as raised:
			apply_settings(
				command_from_state(
					state,
					enabled=not state["enabled"],
					expected_version=cast(int, state["configuration_version"]) + 1,
				),
				"COD-017-stale-version",
				"COD-017-stale-version",
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0005")
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)
		self.assertEqual(current_state(), state)

	def test_noop_keeps_configuration_version_stable(self) -> None:
		state = current_state()
		result = apply_settings(
			command_from_state(state),
			"COD-017-noop-0001",
			"COD-017-noop",
		)
		self.assertFalse(result["changed"])
		self.assertEqual(result["changed_fields"], [])
		self.assertEqual(result["configuration_version"], state["configuration_version"])

	def test_same_idempotency_key_with_different_request_conflicts(self) -> None:
		state = current_state()
		deduplication_id = "COD-017-different-request"
		apply_settings(
			command_from_state(state),
			deduplication_id,
			"COD-017-same-key-first",
		)
		with self.assertRaises(IoneApplicationError) as raised:
			apply_settings(
				command_from_state(
					state,
					integration_timeout_seconds=(31 if state["integration_timeout_seconds"] != 31 else 32),
				),
				deduplication_id,
				"COD-017-same-key-second",
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0007")
		self.assertEqual(current_state(), state)

	def test_direct_document_save_cannot_weaken_fixed_policy(self) -> None:
		doc = frappe.get_single("HRP System Settings")
		doc.strict_data_scope = 0
		with self.assertRaises(IoneApplicationError) as raised:
			doc.save(ignore_permissions=True)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")
		self.assertTrue(current_state()["strict_data_scope"])


class TestSystemSettingsAPI(FrappeAPITestCase):
	def setUp(self) -> None:
		super().setUp()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def test_http_get_and_update_replay_use_public_contract(self) -> None:
		get_response = self.get(self.method(GET_SETTINGS_METHOD))
		self.assertEqual(get_response.status_code, 200, get_response.get_data(as_text=True))
		state = get_response.get_json()["message"]
		self.assertNotIn("configuration_json", state)
		timeout = 31 if state["integration_timeout_seconds"] != 31 else 32
		data = {
			"enabled": int(cast(bool, state["enabled"])),
			"default_company": state["default_company"],
			"default_hospital": state["default_hospital"],
			"integration_timeout_seconds": timeout,
			"remarks": state["remarks"],
			"expected_version": state["configuration_version"],
		}
		headers = {
			"Idempotency-Key": "COD-017-http-key-0001",
			"X-Correlation-ID": "COD-017-http",
		}
		first = self.post(self.method(UPDATE_SETTINGS_METHOD), data, headers=headers)
		second = self.post(self.method(UPDATE_SETTINGS_METHOD), data, headers=headers)
		self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
		self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
		first_payload = first.get_json()["message"]
		second_payload = second.get_json()["message"]
		self.assertFalse(first_payload["idempotency_replayed"])
		self.assertTrue(second_payload["idempotency_replayed"])
		self.assertEqual(
			first_payload["configuration_version"],
			cast(int, state["configuration_version"]) + 1,
		)
		self.assertEqual(
			second_payload["configuration_version"],
			first_payload["configuration_version"],
		)
		self.assertEqual(first.headers["X-Correlation-ID"], "COD-017-http")

	def test_http_update_rejects_missing_idempotency_header(self) -> None:
		state = current_state()
		response = self.post(
			self.method(UPDATE_SETTINGS_METHOD),
			{
				"enabled": int(cast(bool, state["enabled"])),
				"integration_timeout_seconds": state["integration_timeout_seconds"],
				"expected_version": state["configuration_version"],
			},
			headers={"X-Correlation-ID": "COD-017-http-missing-key"},
		)
		self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
		self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0003")
