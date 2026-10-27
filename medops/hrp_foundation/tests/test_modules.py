from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.api.v1.modules import list_modules
from medops.common.domain_service import idempotency_record_name
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_foundation.services.module_settings import (
	SetModuleEnabledService,
)
from medops.hrp_foundation.services.module_settings import (
	set_module_enabled as set_module_enabled_service,
)
from medops.services.module_registry import load_module_registry
from medops.setup.modules import sync_module_defs, sync_module_settings

MODULE_METHOD = "medops.api.v1.modules.set_module_enabled"


def set_module_enabled(
	module_name: str,
	enabled: bool,
	deduplication_id: object | None,
	correlation_id: object | None,
) -> dict[str, object]:
	return set_module_enabled_service(
		module_name,
		enabled,
		idempotency_key=deduplication_id,
		correlation_id=correlation_id,
	)


class TestModuleRegistry(IntegrationTestCase):
	def test_registry_and_site_are_consistent(self) -> None:
		registry = load_module_registry()
		self.assertEqual(len(registry.modules), 36)
		self.assertEqual(len(list_modules()), 36)
		self.assertSetEqual(
			set(
				frappe.get_all(
					"Module Def",
					filters={"app_name": "medops"},
					pluck="module_name",
				)
			),
			set(
				frappe.get_all(
					"HRP Module Setting",
					order_by="sequence asc",
					pluck="module_name",
				)
			),
		)

	def test_sync_is_idempotent_and_repairs_metadata_without_changing_enabled(self) -> None:
		module = load_module_registry().modules[0]
		original_enabled = frappe.db.get_value(
			"HRP Module Setting",
			module.module,
			"enabled",
		)
		frappe.db.set_value(
			"HRP Module Setting",
			module.module,
			"label_cn",
			"drifted label",
			update_modified=False,
		)

		first = sync_module_settings()
		second = sync_module_settings()

		self.assertIn(module.module, first["updated"])
		self.assertEqual(second["created"], [])
		self.assertEqual(second["updated"], [])
		self.assertEqual(len(second["unchanged"]), 36)
		self.assertEqual(
			frappe.db.get_value("HRP Module Setting", module.module, "label_cn"),
			module.label_cn,
		)
		self.assertEqual(
			frappe.db.get_value("HRP Module Setting", module.module, "enabled"),
			original_enabled,
		)

	def test_module_def_conflict_is_preflighted_before_any_write(self) -> None:
		with (
			patch.object(frappe.db, "get_value", return_value="another_app"),
			patch.object(frappe, "get_doc") as get_doc,
			self.assertRaises(IoneApplicationError) as raised,
		):
			sync_module_defs()
		self.assertEqual(raised.exception.code, "IONE-CORE-0009")
		get_doc.assert_not_called()

	def test_module_list_rejects_guest(self) -> None:
		original_user = frappe.session.user or "Administrator"
		try:
			frappe.set_user("Guest")
			with self.assertRaises(IoneApplicationError) as raised:
				list_modules()
			self.assertEqual(raised.exception.code, "IONE-CORE-0001")
		finally:
			frappe.set_user(original_user)

	def test_module_write_requires_module_admin_role(self) -> None:
		module = load_module_registry().modules[0]
		before = frappe.db.count("HRP Service Idempotency")
		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			self.assertRaises(IoneApplicationError) as raised,
		):
			set_module_enabled(
				module.module,
				True,
				"COD-011-permission-key",
				"COD-011-permission",
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0002")
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)

	def test_module_write_is_idempotent_and_audited(self) -> None:
		module = load_module_registry().modules[0]
		original_enabled = bool(frappe.db.get_value("HRP Module Setting", module.module, "enabled"))
		deduplication_id = "COD-011-idempotent-key-0001"
		with patch("medops.services.audit_context.frappe.logger") as logger:
			first = set_module_enabled(
				module.module,
				not original_enabled,
				deduplication_id,
				"COD-011-idempotent-first",
			)
			second = set_module_enabled(
				module.module,
				not original_enabled,
				deduplication_id,
				"COD-011-idempotent-second",
			)
		self.assertTrue(first["changed"])
		self.assertFalse(first["idempotency_replayed"])
		self.assertEqual(second["module"], first["module"])
		self.assertEqual(second["enabled"], first["enabled"])
		self.assertEqual(second["changed"], first["changed"])
		self.assertTrue(second["idempotency_replayed"])
		self.assertNotEqual(second["request_id"], first["request_id"])
		record = frappe.get_doc(
			"HRP Service Idempotency",
			idempotency_record_name(
				SetModuleEnabledService.definition.name,
				deduplication_id,
			),
		)
		self.assertEqual(record.status, "Completed")
		self.assertNotIn(deduplication_id, record.as_json())
		self.assertNotIn(module.module, record.response_snapshot)
		info_events = [call.args[0]["event"] for call in logger.return_value.info.call_args_list]
		self.assertEqual(info_events.count("domain_service_started"), 2)
		self.assertIn("domain_service_completed", info_events)
		self.assertIn("domain_service_replayed", info_events)
		set_module_enabled(
			module.module,
			original_enabled,
			"COD-011-idempotent-restore",
			"COD-011-idempotent-restore",
		)

	def test_module_write_rejects_invalid_input_before_mutation(self) -> None:
		module = load_module_registry().modules[0]
		original_enabled = frappe.db.get_value(
			"HRP Module Setting",
			module.module,
			"enabled",
		)
		with self.assertRaises(IoneApplicationError) as raised:
			set_module_enabled(
				module.module,
				not bool(original_enabled),
				"short",
				"COD-011-invalid-key",
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0003")
		self.assertEqual(
			frappe.db.get_value("HRP Module Setting", module.module, "enabled"),
			original_enabled,
		)

		with self.assertRaises(IoneApplicationError) as raised:
			set_module_enabled(
				"HRP Not Declared",
				True,
				"COD-011-invalid-module-key",
				"COD-011-invalid-module",
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0004")

	def test_same_idempotency_key_with_different_request_is_rejected(self) -> None:
		module = load_module_registry().modules[0]
		original_enabled = bool(frappe.db.get_value("HRP Module Setting", module.module, "enabled"))
		deduplication_id = "COD-011-conflict-key-0001"
		set_module_enabled(
			module.module,
			original_enabled,
			deduplication_id,
			"COD-011-conflict-first",
		)

		with self.assertRaises(IoneApplicationError) as raised:
			set_module_enabled(
				module.module,
				not original_enabled,
				deduplication_id,
				"COD-011-conflict-second",
			)

		self.assertEqual(raised.exception.code, "IONE-CORE-0007")
		self.assertEqual(
			bool(frappe.db.get_value("HRP Module Setting", module.module, "enabled")),
			original_enabled,
		)

	def test_completed_idempotency_record_is_immutable_and_tamper_fails_closed(self) -> None:
		module = load_module_registry().modules[0]
		original_enabled = bool(frappe.db.get_value("HRP Module Setting", module.module, "enabled"))
		deduplication_id = "COD-011-tamper-key-0001"
		set_module_enabled(
			module.module,
			original_enabled,
			deduplication_id,
			"COD-011-tamper-first",
		)
		record_name = idempotency_record_name(
			SetModuleEnabledService.definition.name,
			deduplication_id,
		)
		record = frappe.get_doc("HRP Service Idempotency", record_name)
		record.service_name = "hrp_foundation.tampered.execute"
		with self.assertRaises(IoneApplicationError) as raised:
			record.save(ignore_permissions=True)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")

		frappe.db.set_value(
			"HRP Service Idempotency",
			record_name,
			"response_snapshot",
			"tampered-ciphertext",
			update_modified=False,
		)
		with self.assertRaises(IoneApplicationError) as raised:
			set_module_enabled(
				module.module,
				original_enabled,
				deduplication_id,
				"COD-011-tamper-replay",
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0009")
		self.assertEqual(
			bool(frappe.db.get_value("HRP Module Setting", module.module, "enabled")),
			original_enabled,
		)

	def test_expired_idempotency_key_can_be_reused_for_a_new_request(self) -> None:
		module = load_module_registry().modules[0]
		original_enabled = bool(frappe.db.get_value("HRP Module Setting", module.module, "enabled"))
		deduplication_id = "COD-011-expired-key-0001"
		first = set_module_enabled(
			module.module,
			not original_enabled,
			deduplication_id,
			"COD-011-expired-first",
		)
		record_name = idempotency_record_name(
			SetModuleEnabledService.definition.name,
			deduplication_id,
		)
		frappe.db.set_value(
			"HRP Service Idempotency",
			record_name,
			"expires_at",
			"2000-01-01 00:00:00",
			update_modified=False,
		)

		second = set_module_enabled(
			module.module,
			original_enabled,
			deduplication_id,
			"COD-011-expired-second",
		)

		self.assertFalse(second["idempotency_replayed"])
		self.assertNotEqual(second["request_id"], first["request_id"])
		self.assertEqual(second["enabled"], original_enabled)

	def test_missing_idempotency_key_is_rejected(self) -> None:
		module = load_module_registry().modules[0]
		with self.assertRaises(IoneApplicationError) as raised:
			set_module_enabled(
				module.module,
				True,
				None,
				"COD-011-missing-key",
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0003")


class TestModuleAPI(FrappeAPITestCase):
	def setUp(self) -> None:
		super().setUp()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def test_http_command_reads_idempotency_header_and_replays_response(self) -> None:
		module = load_module_registry().modules[0]
		original_enabled = bool(frappe.db.get_value("HRP Module Setting", module.module, "enabled"))
		data = {
			"module_name": module.module,
			"enabled": int(not original_enabled),
		}
		headers = {
			# HTTP writes commit independently. A previous suite's receipt must
			# not turn this run's first command into a replay or a conflict.
			"Idempotency-Key": "COD-011-http-" + uuid4().hex,
			"X-Correlation-ID": "COD-011-http",
		}

		first = self.post(self.method(MODULE_METHOD), data, headers=headers)
		second = self.post(self.method(MODULE_METHOD), data, headers=headers)

		self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
		self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
		first_payload = first.get_json()["message"]
		second_payload = second.get_json()["message"]
		self.assertFalse(first_payload["idempotency_replayed"])
		self.assertTrue(second_payload["idempotency_replayed"])
		self.assertEqual(first_payload["module"], second_payload["module"])
		self.assertEqual(first_payload["enabled"], second_payload["enabled"])
		self.assertNotEqual(first_payload["request_id"], second_payload["request_id"])
		self.assertEqual(first.headers["X-Correlation-ID"], "COD-011-http")
		self.assertEqual(second.headers["X-Correlation-ID"], "COD-011-http")

	def test_http_command_rejects_missing_idempotency_header(self) -> None:
		module = load_module_registry().modules[0]
		original_enabled = bool(frappe.db.get_value("HRP Module Setting", module.module, "enabled"))
		response = self.post(
			self.method(MODULE_METHOD),
			{
				"module_name": module.module,
				"enabled": int(not original_enabled),
			},
			headers={"X-Correlation-ID": "COD-011-http-missing-key"},
		)

		self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
		self.assertEqual(
			response.headers["X-Ione-Error-Code"],
			"IONE-CORE-0003",
		)
		self.assertEqual(
			bool(frappe.db.get_value("HRP Module Setting", module.module, "enabled")),
			original_enabled,
		)
