from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.test_api import FrappeAPITestCase

from medops.common.domain_service import (
	DomainServiceDefinition,
	idempotency_record_name,
)
from medops.common.error_catalog import IoneApplicationError
from medops.common.transactional_message import (
	BASE_MESSAGE_FIELDS,
	MessageBoxDefinition,
	processing_token_hash,
)
from medops.services.transactional_message import (
	BeginInboxService,
	ClaimOutboxMessage,
	ClaimOutboxService,
	CompleteInboxService,
	CompleteMessage,
	CompleteOutboxService,
	FailInboxService,
	FailMessage,
	FailOutboxService,
	MessageEnvelope,
	PublishOutboxService,
	TransactionalMessageDocument,
	get_transactional_message_contract_status,
)

TEST_OUTBOX = "HRP Test Transactional Outbox"
TEST_INBOX = "HRP Test Transactional Inbox"
MESSAGE_METHOD = "medops.api.v1.messages.get_transactional_message_contract"
REQUIRED_ROLES = frozenset({"System Manager"})
OUTBOX_DEFINITION = MessageBoxDefinition(
	doctype=TEST_OUTBOX,
	kind="outbox",
	required_roles=REQUIRED_ROLES,
	max_attempts=2,
	default_lease_seconds=30,
)
INBOX_DEFINITION = MessageBoxDefinition(
	doctype=TEST_INBOX,
	kind="inbox",
	required_roles=REQUIRED_ROLES,
	max_attempts=2,
	default_lease_seconds=30,
)


class TestOutboxDocument(TransactionalMessageDocument):
	message_box_definition = OUTBOX_DEFINITION


class TestInboxDocument(TransactionalMessageDocument):
	message_box_definition = INBOX_DEFINITION


class PublishTestOutboxService(PublishOutboxService):
	message_box_definition = OUTBOX_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_integration.test_outbox.publish",
		version=1,
		kind="command",
		required_roles=REQUIRED_ROLES,
	)


class ClaimTestOutboxService(ClaimOutboxService):
	message_box_definition = OUTBOX_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_integration.test_outbox.claim",
		version=1,
		kind="command",
		required_roles=REQUIRED_ROLES,
	)


class CompleteTestOutboxService(CompleteOutboxService):
	message_box_definition = OUTBOX_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_integration.test_outbox.complete",
		version=1,
		kind="command",
		required_roles=REQUIRED_ROLES,
	)


class FailTestOutboxService(FailOutboxService):
	message_box_definition = OUTBOX_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_integration.test_outbox.fail",
		version=1,
		kind="command",
		required_roles=REQUIRED_ROLES,
	)


class BeginTestInboxService(BeginInboxService):
	message_box_definition = INBOX_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_integration.test_inbox.begin",
		version=1,
		kind="command",
		required_roles=REQUIRED_ROLES,
	)


class CompleteTestInboxService(CompleteInboxService):
	message_box_definition = INBOX_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_integration.test_inbox.complete",
		version=1,
		kind="command",
		required_roles=REQUIRED_ROLES,
	)


class FailTestInboxService(FailInboxService):
	message_box_definition = INBOX_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_integration.test_inbox.fail",
		version=1,
		kind="command",
		required_roles=REQUIRED_ROLES,
	)


MESSAGE_SERVICES = (
	PublishTestOutboxService,
	ClaimTestOutboxService,
	CompleteTestOutboxService,
	FailTestOutboxService,
	BeginTestInboxService,
	CompleteTestInboxService,
	FailTestInboxService,
)


def _test_doctype_payload(definition: MessageBoxDefinition) -> dict[str, object]:
	fields: list[dict[str, object]] = []
	for contract in BASE_MESSAGE_FIELDS:
		field: dict[str, object] = {
			"fieldname": contract.fieldname,
			"fieldtype": contract.fieldtype,
			"label": contract.fieldname.replace("_", " ").title(),
			"read_only": 1,
			"reqd": int(contract.required and not contract.hidden),
			"hidden": int(contract.hidden),
			"search_index": int(contract.search_index),
		}
		if contract.fieldname == "status":
			field["options"] = "\n".join(definition.statuses)
		elif contract.fieldname in {"payload_json", "result_json"}:
			field["options"] = "JSON"
		elif contract.fieldname == "reference_doctype":
			field["options"] = "DocType"
		elif contract.fieldname == "reference_name":
			field["options"] = "reference_doctype"
		elif contract.fieldname == "attempt_count":
			field["default"] = "0"
		if definition.kind == "outbox" and contract.fieldname == "event_id":
			field["unique"] = 1
		if contract.fieldname in {"event_id", "event_type", "status"}:
			field["in_list_view"] = 1
		fields.append(field)
	return {
		"doctype": "DocType",
		"name": definition.doctype,
		"module": "Custom",
		"custom": 1,
		"engine": "InnoDB",
		"autoname": "Prompt",
		"naming_rule": "Set by user",
		"title_field": "event_id",
		"allow_rename": 0,
		"is_submittable": 0,
		"issingle": 0,
		"istable": 0,
		"track_changes": 0,
		"fields": fields,
		"permissions": [
			{
				"role": "System Manager",
				"read": 1,
				"write": 0,
				"create": 0,
				"delete": 0,
				"submit": 0,
				"cancel": 0,
				"amend": 0,
				"import": 0,
				"report": 1,
				"select": 1,
			}
		],
	}


def _envelope(label: str, *, payload: dict[str, object] | None = None) -> MessageEnvelope:
	return MessageEnvelope(
		event_id=f"evt-COD-013-{label}",
		event_type="hrp.test.changed",
		source="hrp-test-producer",
		destination="hrp-test-consumer",
		occurred_at="2026-07-30 10:00:00",
		payload=payload or {"amount": 20, "ward": "A"},
		payload_schema_version=1,
		reference_doctype="User",
		reference_name="Administrator",
	)


def _key(label: str) -> str:
	return f"COD-013-{label}-key"


def _correlation(label: object) -> str:
	return "-".join(("COD", "013", str(label)))


def _result(execution: dict[str, Any]) -> dict[str, Any]:
	return cast(dict[str, Any], execution["result"])


class TestTransactionalMessage(IntegrationTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		for definition, controller in (
			(OUTBOX_DEFINITION, TestOutboxDocument),
			(INBOX_DEFINITION, TestInboxDocument),
		):
			if frappe.db.exists("DocType", definition.doctype):
				frappe.db.delete(definition.doctype)
				frappe.delete_doc(
					"DocType",
					definition.doctype,
					ignore_permissions=True,
					force=True,
					delete_permanently=True,
				)
			frappe.get_doc(_test_doctype_payload(definition)).insert(ignore_permissions=True)
			frappe.controllers.setdefault(frappe.local.site, {})[definition.doctype] = controller
		frappe.local.db.commit()
		cls.addClassCleanup(cls._drop_test_doctypes)

	@classmethod
	def _drop_test_doctypes(cls) -> None:
		del cls
		for service in MESSAGE_SERVICES:
			frappe.db.delete(
				"HRP Service Idempotency",
				{"service_name": service.definition.name},
			)
		for definition in (OUTBOX_DEFINITION, INBOX_DEFINITION):
			frappe.controllers.setdefault(frappe.local.site, {}).pop(
				definition.doctype,
				None,
			)
			if frappe.db.exists("DocType", definition.doctype):
				frappe.db.delete(definition.doctype)
				frappe.delete_doc(
					"DocType",
					definition.doctype,
					ignore_permissions=True,
					force=True,
					delete_permanently=True,
				)
		frappe.local.db.commit()

	def _publish(self, label: str) -> dict[str, Any]:
		return (
			PublishTestOutboxService()
			.execute(
				_envelope(label),
				idempotency_key=_key(f"publish-{label}"),
				correlation_id=_correlation(f"publish-{label}"),
			)
			.as_public_dict()
		)

	def _claim(self, message: str, label: str) -> dict[str, Any]:
		return (
			ClaimTestOutboxService()
			.execute(
				ClaimOutboxMessage(message, "worker-01", 30),
				idempotency_key=_key(f"claim-{label}"),
				correlation_id=_correlation(f"claim-{label}"),
			)
			.as_public_dict()
		)

	def _begin(self, label: str, *, payload: dict[str, object] | None = None) -> dict[str, Any]:
		variant = "custom" if payload is not None else "default"
		return (
			BeginTestInboxService()
			.execute(
				_envelope(label, payload=payload),
				idempotency_key=_key(f"begin-{label}-{variant}"),
				correlation_id=_correlation(f"begin-{label}"),
			)
			.as_public_dict()
		)

	def test_outbox_publish_is_transactional_idempotent_and_service_only(self) -> None:
		label = "publish-idempotent"
		envelope = _envelope(label)
		first = (
			PublishTestOutboxService()
			.execute(
				envelope,
				idempotency_key=_key("publish-idempotent"),
				correlation_id=_correlation("publish-idempotent"),
			)
			.as_public_dict()
		)
		second = (
			PublishTestOutboxService()
			.execute(
				envelope,
				idempotency_key=_key("publish-idempotent"),
				correlation_id=_correlation("publish-idempotent-replay"),
			)
			.as_public_dict()
		)
		first_result = _result(first)
		second_result = _result(second)
		message = first_result["message"]
		self.assertFalse(first["idempotency_replayed"])
		self.assertTrue(second["idempotency_replayed"])
		self.assertEqual(message, second_result["message"])
		self.assertEqual(frappe.db.count(TEST_OUTBOX, {"name": message}), 1)
		document = frappe.get_doc(TEST_OUTBOX, message)
		self.assertEqual(document.payload_json, '{"amount":20,"ward":"A"}')
		self.assertRegex(document.payload_hash, r"^[0-9a-f]{64}$")

		direct = frappe.get_doc(
			{
				"doctype": TEST_OUTBOX,
				"name": "out-" + "a" * 64,
				**document.as_dict(),
			}
		)
		direct.name = "out-" + "a" * 64
		direct.event_id = "evt-COD-013-direct-insert"
		with self.assertRaises(IoneApplicationError) as raised:
			direct.insert(ignore_permissions=True)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")

		document.destination = "changed-consumer"
		with self.assertRaises(IoneApplicationError) as raised:
			document.save(ignore_permissions=True)
		self.assertEqual(raised.exception.code, "IONE-CORE-0008")
		with self.assertRaises(IoneApplicationError):
			document.db_set("status", "Failed")
		with self.assertRaises(IoneApplicationError):
			frappe.delete_doc(TEST_OUTBOX, message, ignore_permissions=True, force=True)

	def test_permission_is_checked_before_outbox_idempotency_reservation(self) -> None:
		key = _key("permission")
		reservation = idempotency_record_name(
			PublishTestOutboxService.definition.name,
			key,
		)
		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			self.assertRaises(IoneApplicationError) as raised,
		):
			PublishTestOutboxService().execute(
				_envelope("permission"),
				idempotency_key=key,
				correlation_id=_correlation("permission"),
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0002")
		self.assertFalse(frappe.db.exists("HRP Service Idempotency", reservation))

	def test_outbox_claim_token_and_completion_are_locked_and_idempotent(self) -> None:
		message = _result(self._publish("delivery"))["message"]
		claim = _result(self._claim(message, "delivery"))
		token = claim["processing_token"]
		document = frappe.get_doc(TEST_OUTBOX, message)
		self.assertEqual(document.status, "Processing")
		self.assertEqual(document.processing_token_hash, processing_token_hash(token))
		self.assertNotEqual(document.processing_token_hash, token)

		with self.assertRaises(IoneApplicationError) as raised:
			CompleteTestOutboxService().execute(
				CompleteMessage(message, "X" * 43),
				idempotency_key=_key("complete-wrong-token"),
				correlation_id=_correlation("complete-wrong-token"),
			)
		self.assertEqual(raised.exception.code, "IONE-CORE-0005")

		service = CompleteTestOutboxService()
		command = CompleteMessage(message, token)
		first = service.execute(
			command,
			idempotency_key=_key("complete-delivery"),
			correlation_id=_correlation("complete-delivery"),
		).as_public_dict()
		second = service.execute(
			command,
			idempotency_key=_key("complete-delivery"),
			correlation_id=_correlation("complete-delivery-replay"),
		).as_public_dict()
		self.assertEqual(_result(first)["status"], "Delivered")
		self.assertTrue(second["idempotency_replayed"])
		document.reload()
		self.assertFalse(document.processing_token_hash)
		self.assertTrue(document.completed_at)

	def test_outbox_failure_retries_then_dead_letters_without_payload_audit(self) -> None:
		message = _result(self._publish("dead-letter"))["message"]
		with patch("medops.services.audit_context.frappe.logger") as logger:
			for attempt in (1, 2):
				claim = _result(self._claim(message, f"dead-letter-{attempt}"))
				failed = (
					FailTestOutboxService()
					.execute(
						FailMessage(
							message,
							claim["processing_token"],
							"REMOTE_TIMEOUT",
							"Remote endpoint timed out",
							0,
						),
						idempotency_key=_key(f"fail-dead-letter-{attempt}"),
						correlation_id=_correlation(f"fail-dead-letter-{attempt}"),
					)
					.as_public_dict()
				)
		self.assertEqual(_result(failed)["status"], "Dead Letter")
		document = frappe.get_doc(TEST_OUTBOX, message)
		self.assertEqual(document.attempt_count, 2)
		self.assertTrue(document.completed_at)
		audit_text = str(logger.mock_calls)
		self.assertNotIn("Remote endpoint timed out", audit_text)
		self.assertNotIn('{"amount":20', audit_text)

		with self.assertRaises(IoneApplicationError) as raised:
			self._claim(message, "dead-letter-after-terminal")
		self.assertEqual(raised.exception.code, "IONE-CORE-0006")

	def test_outbox_row_lock_contention_fails_fast_and_rolls_back_reservation(self) -> None:
		message = _result(self._publish("lock"))["message"]
		self._primary_connection.commit()
		key = _key("claim-lock")
		reservation = idempotency_record_name(
			ClaimTestOutboxService.definition.name,
			key,
		)
		with self.primary_connection():
			self.assertEqual(
				frappe.db.get_value(TEST_OUTBOX, message, "name", for_update=True),
				message,
			)
			with self.secondary_connection(), self.assertRaises(IoneApplicationError) as raised:
				ClaimTestOutboxService().execute(
					ClaimOutboxMessage(message, "worker-lock", 30),
					idempotency_key=key,
					correlation_id=_correlation("claim-lock"),
				)
			self.assertEqual(raised.exception.code, "IONE-CORE-0005")
		with self.secondary_connection():
			self.assertFalse(frappe.db.exists("HRP Service Idempotency", reservation))
			self.assertEqual(frappe.db.get_value(TEST_OUTBOX, message, "status"), "Pending")
		self._primary_connection.rollback()

	def test_inbox_consumer_event_dedup_replays_processed_result(self) -> None:
		begin = _result(self._begin("inbox-dedup"))
		completed = (
			CompleteTestInboxService()
			.execute(
				CompleteMessage(
					begin["message"],
					begin["processing_token"],
					{"created": 2},
				),
				idempotency_key=_key("inbox-complete"),
				correlation_id=_correlation("inbox-complete"),
			)
			.as_public_dict()
		)
		replay = (
			BeginTestInboxService()
			.execute(
				_envelope("inbox-dedup"),
				idempotency_key=_key("inbox-duplicate-new-request"),
				correlation_id=_correlation("inbox-duplicate"),
			)
			.as_public_dict()
		)
		completed_result = _result(completed)
		replay_result = _result(replay)
		self.assertEqual(completed_result["status"], "Processed")
		self.assertFalse(replay_result["should_process"])
		self.assertTrue(replay_result["duplicate"])
		self.assertEqual(replay_result["result"], {"created": 2})
		self.assertEqual(frappe.db.count(TEST_INBOX), 1)

	def test_inbox_same_event_with_different_payload_is_rejected(self) -> None:
		begin = _result(self._begin("inbox-conflict"))
		CompleteTestInboxService().execute(
			CompleteMessage(
				begin["message"],
				begin["processing_token"],
				{"ok": True},
			),
			idempotency_key=_key("inbox-conflict-complete"),
			correlation_id=_correlation("inbox-conflict-complete"),
		)
		with self.assertRaises(IoneApplicationError) as raised:
			self._begin("inbox-conflict", payload={"amount": 999})
		self.assertEqual(raised.exception.code, "IONE-CORE-0007")

	def test_inbox_failure_retries_then_becomes_ignored(self) -> None:
		first = _result(self._begin("inbox-ignore"))
		for attempt, started in (
			(1, first),
			(2, None),
		):
			if started is None:
				started = _result(
					BeginTestInboxService()
					.execute(
						_envelope("inbox-ignore"),
						idempotency_key=_key("inbox-ignore-retry"),
						correlation_id=_correlation("inbox-ignore-retry"),
					)
					.as_public_dict()
				)
			failed = (
				FailTestInboxService()
				.execute(
					FailMessage(
						started["message"],
						started["processing_token"],
						"HANDLER_FAILED",
						"Consumer handler failed",
						0,
					),
					idempotency_key=_key(f"inbox-ignore-fail-{attempt}"),
					correlation_id=_correlation(f"inbox-ignore-fail-{attempt}"),
				)
				.as_public_dict()
			)
		self.assertEqual(_result(failed)["status"], "Ignored")
		replay = _result(
			BeginTestInboxService()
			.execute(
				_envelope("inbox-ignore"),
				idempotency_key=_key("inbox-ignore-terminal-replay"),
				correlation_id=_correlation("inbox-ignore-terminal-replay"),
			)
			.as_public_dict()
		)
		self.assertFalse(replay["should_process"])
		self.assertEqual(replay["status"], "Ignored")

	def test_contract_status_requires_system_role_and_exposes_no_write_api(self) -> None:
		contract = get_transactional_message_contract_status()
		self.assertEqual(contract["schema_version"], 1)
		self.assertFalse(contract["http_write_enabled"])
		self.assertEqual(
			contract["delivery_semantics"]["inbox_deduplication"],
			"consumer_and_event_id",
		)
		with (
			patch("medops.services.errors.frappe.get_roles", return_value=["HRP User"]),
			self.assertRaises(IoneApplicationError) as raised,
		):
			get_transactional_message_contract_status()
		self.assertEqual(raised.exception.code, "IONE-CORE-0002")


class TestTransactionalMessageAPI(FrappeAPITestCase):
	def setUp(self) -> None:
		super().setUp()
		self.TEST_CLIENT.set_cookie(key="sid", value=self.sid)

	def test_http_contract_is_read_only_and_returns_delivery_semantics(self) -> None:
		response = self.get(self.method(MESSAGE_METHOD))

		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		payload = response.get_json()["message"]
		self.assertFalse(payload["http_write_enabled"])
		self.assertEqual(payload["delivery_semantics"]["outbox_delivery"], "at_least_once")
		self.assertEqual(
			payload["delivery_semantics"]["claiming"],
			"row_lock_nowait_and_lease",
		)

	def test_http_contract_rejects_guest(self) -> None:
		self.TEST_CLIENT.set_cookie(key="sid", value="Guest")
		response = self.get(self.method(MESSAGE_METHOD))

		self.assertEqual(response.status_code, 401, response.get_data(as_text=True))
		self.assertEqual(response.headers["X-Ione-Error-Code"], "IONE-CORE-0001")
