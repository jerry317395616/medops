from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from medops.common.domain_service import (
	DomainServiceDefinition,
	idempotency_record_name,
)
from medops.common.error_catalog import IoneApplicationError
from medops.services.domain_service import DomainService
from medops.services.module_registry import load_module_registry


@dataclass(frozen=True, slots=True)
class FailingCommand:
	module_name: str


class FailingDomainService(DomainService[FailingCommand]):
	definition = DomainServiceDefinition(
		name="hrp_foundation.test.rollback",
		version=1,
		kind="command",
		required_roles=frozenset({"System Manager"}),
	)

	def request_payload(self, command: FailingCommand) -> dict[str, object]:
		return {"module_name": command.module_name}

	def perform(self, command: FailingCommand) -> dict[str, object]:
		frappe.db.set_value(
			"HRP Module Setting",
			command.module_name,
			"workspace_route",
			"cod-011-must-rollback",
		)
		raise RuntimeError("synthetic service failure")


class InvalidResultDomainService(DomainService[FailingCommand]):
	definition = DomainServiceDefinition(
		name="hrp_foundation.test.invalid_result",
		version=1,
		kind="command",
		required_roles=frozenset({"System Manager"}),
	)

	def request_payload(self, command: FailingCommand) -> dict[str, object]:
		return {"module_name": command.module_name}

	def perform(self, command: FailingCommand) -> dict[str, object]:
		return {"unsupported": object()}


class ModuleCountQueryService(DomainService[None]):
	definition = DomainServiceDefinition(
		name="hrp_foundation.test.module_count",
		version=1,
		kind="query",
		required_roles=frozenset({"System Manager"}),
	)

	def request_payload(self, command: None) -> dict[str, object]:
		del command
		return {}

	def perform(self, command: None) -> dict[str, object]:
		del command
		return {"module_count": frappe.db.count("HRP Module Setting")}


class TestDomainService(IntegrationTestCase):
	def test_nested_service_failure_restores_its_own_boundary_and_preserves_earlier_caller_write(self):
		modules = [row.module for row in load_module_registry().modules[:3]]
		original = [frappe.db.get_value("HRP Module Setting", name, "workspace_route") for name in modules]
		outer_key = "COD033-nested-outer-" + frappe.generate_hash(length=12)
		inner_key = "COD033-nested-inner-" + frappe.generate_hash(length=12)

		class Child(DomainService[FailingCommand]):
			definition = DomainServiceDefinition(
				name="hrp_foundation.test.nested_child",
				version=1,
				kind="command",
				required_roles=frozenset({"System Manager"}),
			)

			def request_payload(self, command):
				return dict(module=command.module_name)

			def perform(self, command):
				frappe.db.set_value(
					"HRP Module Setting", command.module_name, "workspace_route", "COD033-nested-child"
				)
				return dict(changed=True)

		class Parent(Child):
			definition = DomainServiceDefinition(
				name="hrp_foundation.test.nested_parent",
				version=1,
				kind="command",
				required_roles=frozenset({"System Manager"}),
			)

			def perform(self, command):
				frappe.db.set_value(
					"HRP Module Setting", command.module_name, "workspace_route", "COD033-nested-parent"
				)
				Child().execute(FailingCommand(modules[2]), idempotency_key=inner_key)
				raise RuntimeError("synthetic parent failure after successful nested service")

		frappe.db.set_value(
			"HRP Module Setting", modules[0], "workspace_route", "COD033-earlier-caller-write"
		)
		with self.assertRaises(IoneApplicationError):
			Parent().execute(FailingCommand(modules[1]), idempotency_key=outer_key)
		self.assertEqual(
			frappe.db.get_value("HRP Module Setting", modules[0], "workspace_route"),
			"COD033-earlier-caller-write",
		)
		self.assertEqual(
			[frappe.db.get_value("HRP Module Setting", name, "workspace_route") for name in modules[1:]],
			original[1:],
		)
		for definition, key in ((Parent.definition, outer_key), (Child.definition, inner_key)):
			self.assertFalse(
				frappe.db.exists("HRP Service Idempotency", idempotency_record_name(definition.name, key))
			)

	def test_database_conflict_preserves_outer_retry_instead_of_missing_savepoint(self) -> None:
		with (
			patch.object(
				ModuleCountQueryService,
				"perform",
				side_effect=frappe.QueryDeadlockError("synthetic conflict"),
			),
			patch.object(ModuleCountQueryService, "_rollback") as rollback,
			self.assertRaises(frappe.QueryDeadlockError),
		):
			ModuleCountQueryService().execute(None)
		rollback.assert_not_called()

	def test_query_does_not_require_or_persist_idempotency(self) -> None:
		before = frappe.db.count("HRP Service Idempotency")

		execution = ModuleCountQueryService().execute(
			None,
			correlation_id="COD-011-query",
		)

		self.assertEqual(execution.result["module_count"], 36)
		self.assertFalse(execution.idempotency_replayed)
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before)

	def test_unknown_failure_rolls_back_domain_write_and_reservation(self) -> None:
		module_name = load_module_registry().modules[0].module
		original_route = frappe.db.get_value(
			"HRP Module Setting",
			module_name,
			"workspace_route",
		)
		deduplication_id = "COD-011-rollback-0001"
		with (
			patch("medops.services.audit_context.frappe.logger") as logger,
			self.assertRaises(IoneApplicationError) as raised,
		):
			FailingDomainService().execute(
				FailingCommand(module_name),
				correlation_id="COD-011-rollback",
				idempotency_key=deduplication_id,
			)

		self.assertEqual(raised.exception.code, "IONE-CORE-0012")
		self.assertEqual(
			frappe.db.get_value("HRP Module Setting", module_name, "workspace_route"),
			original_route,
		)
		self.assertFalse(
			frappe.db.exists(
				"HRP Service Idempotency",
				idempotency_record_name(
					FailingDomainService.definition.name,
					deduplication_id,
				),
			)
		)
		failure = logger.return_value.warning.call_args.args[0]
		self.assertEqual(failure["event"], "domain_service_failed")
		self.assertEqual(failure["cause_type"], "RuntimeError")
		self.assertNotIn("synthetic", str(failure))

	def test_invalid_result_is_controlled_and_rolls_back_reservation(self) -> None:
		module_name = load_module_registry().modules[0].module
		deduplication_id = "COD-011-invalid-result-0001"
		with self.assertRaises(IoneApplicationError) as raised:
			InvalidResultDomainService().execute(
				FailingCommand(module_name),
				correlation_id="COD-011-invalid-result",
				idempotency_key=deduplication_id,
			)

		self.assertEqual(raised.exception.code, "IONE-CORE-0003")
		self.assertFalse(
			frappe.db.exists(
				"HRP Service Idempotency",
				idempotency_record_name(
					InvalidResultDomainService.definition.name,
					deduplication_id,
				),
			)
		)
