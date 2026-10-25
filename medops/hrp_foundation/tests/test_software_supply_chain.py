from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.error_catalog import IoneApplicationError
from medops.common.software_supply_chain import SoftwareSupplyChainContractError
from medops.services.software_supply_chain import (
	get_software_supply_chain_contract_status,
)

SECURITY_METHOD = "medops.api.v1.security.get_software_supply_chain_contract"


class TestSoftwareSupplyChainContract(IntegrationTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.original_user = frappe.session.user or "Administrator"
		frappe.set_user("Administrator")

	def tearDown(self) -> None:
		frappe.set_user(self.original_user)
		super().tearDown()

	def test_contract_is_read_only_redacted_and_unavailable_for_site_execution(self) -> None:
		contract = cast(dict[str, Any], get_software_supply_chain_contract_status())
		self.assertEqual(contract["status"], "ok")
		self.assertEqual(contract["schema_version"], 1)
		self.assertEqual(contract["app"], "medops")
		self.assertEqual(contract["sbom"]["format"], "CycloneDX JSON")
		self.assertEqual(contract["sbom"]["spec_version"], "1.7")
		self.assertFalse(contract["sbom"]["contains_personal_data"])
		self.assertFalse(contract["execution_policy"]["http_write_enabled"])
		self.assertFalse(contract["execution_policy"]["site_execution_enabled"])
		self.assertFalse(contract["execution_policy"]["production_execution_enabled"])
		self.assertFalse(contract["scan_available_from_site"])
		self.assertEqual(contract["artifact_storage"], "ci_or_release_artifact")
		self.assertEqual(contract["idempotency"], "read_only_replay_safe")

	def test_permission_is_checked_before_policy_load(self) -> None:
		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			patch("medops.services.software_supply_chain.load_software_supply_chain_policy") as load_policy,
			self.assertRaises(IoneApplicationError) as raised,
		):
			get_software_supply_chain_contract_status()
		self.assertEqual(raised.exception.code, "IONE-CORE-0002")
		load_policy.assert_not_called()

	def test_invalid_policy_maps_to_stable_configuration_error(self) -> None:
		with (
			patch(
				"medops.services.software_supply_chain.load_software_supply_chain_policy",
				side_effect=SoftwareSupplyChainContractError("local source path and secret must not leak"),
			),
			self.assertRaises(IoneApplicationError) as raised,
		):
			get_software_supply_chain_contract_status()
		self.assertEqual(raised.exception.code, "IONE-CORE-0009")
		self.assertNotIn("local source path", str(raised.exception))
		self.assertNotIn("secret", str(raised.exception).lower())

	def test_repeated_reads_are_idempotent_and_do_not_create_records(self) -> None:
		before = {
			"Version": frappe.db.count("Version"),
			"Comment": frappe.db.count("Comment"),
			"Error Log": frappe.db.count("Error Log"),
			"HRP Service Idempotency": frappe.db.count("HRP Service Idempotency"),
		}
		first = get_software_supply_chain_contract_status()
		second = get_software_supply_chain_contract_status()
		after = {
			"Version": frappe.db.count("Version"),
			"Comment": frappe.db.count("Comment"),
			"Error Log": frappe.db.count("Error Log"),
			"HRP Service Idempotency": frappe.db.count("HRP Service Idempotency"),
		}
		self.assertEqual(first, second)
		self.assertEqual(before, after)

	def test_audit_contains_only_governed_metadata(self) -> None:
		with patch("medops.services.audit_context.frappe.logger") as logger:
			contract = get_software_supply_chain_contract_status()
			audit_payload = logger.return_value.info.call_args.args[0]
		self.assertEqual(audit_payload["event"], "software_supply_chain_contract_read")
		self.assertEqual(audit_payload["policy_sha256"], contract["sha256"])
		self.assertEqual(audit_payload["tool_count"], len(contract["tools"]))
		self.assertEqual(audit_payload["exception_count"], contract["exception_count"])
		self.assertTrue(audit_payload["external_execution_only"])
		serialized = json.dumps(audit_payload, ensure_ascii=False)
		self.assertNotIn(frappe.local.site, serialized)
		self.assertNotIn("Administrator", serialized)
		self.assertNotIn("/api/method/", serialized)
		self.assertNotIn("token", serialized.lower())
		self.assertNotIn("password", serialized.lower())


class TestSoftwareSupplyChainAPI(FrappeAPITestCase):
	def setUp(self) -> None:
		super().setUp()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def test_http_contract_returns_governed_read_only_policy(self) -> None:
		response = self.get(self.method(SECURITY_METHOD))
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		payload = response.get_json()["message"]
		self.assertEqual(payload["app"], "medops")
		self.assertEqual(payload["sbom"]["spec_version"], "1.7")
		self.assertFalse(payload["scan_available_from_site"])
		self.assertFalse(payload["execution_policy"]["http_write_enabled"])
		self.assertFalse(payload["execution_policy"]["site_execution_enabled"])
		self.assertFalse(payload["execution_policy"]["production_execution_enabled"])

	def test_http_contract_rejects_guest(self) -> None:
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		response = self.get(self.method(SECURITY_METHOD))
		self.assertEqual(response.status_code, 401, response.get_data(as_text=True))
		self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0001")
