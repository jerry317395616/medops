from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from medops.common.audit_context import normalize_audit_fields
from medops.common.transactional_message import (
	BASE_MESSAGE_FIELDS,
	INBOX_STATUSES,
	OUTBOX_STATUSES,
	MessageBoxDefinition,
	TransactionalMessageContractError,
	get_transactional_message_public_contract,
	message_record_name,
	normalize_error_details,
	normalize_message_snapshot,
	normalize_optional_result,
	normalize_payload,
	processing_token_hash,
	processing_token_matches,
)


class TransactionalMessageContractTest(unittest.TestCase):
	def test_definition_is_immutable_and_validates_operational_limits(self) -> None:
		definition = MessageBoxDefinition(
			doctype="HRP Test Outbox",
			kind="outbox",
			required_roles=frozenset({"System Manager"}),
		)
		self.assertEqual(definition.statuses, OUTBOX_STATUSES)
		with self.assertRaises(FrozenInstanceError):
			definition.max_attempts = 20  # type: ignore[misc]

		base = {
			"doctype": "HRP Test Inbox",
			"kind": "inbox",
			"required_roles": frozenset({"System Manager"}),
		}
		for override in (
			{"doctype": "Unsafe"},
			{"kind": "queue"},
			{"required_roles": set()},
			{"max_attempts": 0},
			{"default_lease_seconds": 10},
		):
			with self.subTest(override=override), self.assertRaises(TransactionalMessageContractError):
				MessageBoxDefinition(**{**base, **override})

	def test_payload_is_canonical_and_hash_is_deterministic(self) -> None:
		first = normalize_payload({"ward": "A", "items": [2, 1], "cost_center": "C01"})
		second = normalize_payload('{"cost_center":"C01","items":[2,1],"ward":"A"}')
		self.assertEqual(first, second)
		self.assertEqual(
			first[1],
			'{"cost_center":"C01","items":[2,1],"ward":"A"}',
		)
		self.assertRegex(first[2], r"^[0-9a-f]{64}$")

	def test_payload_rejects_non_object_invalid_json_and_oversized_data(self) -> None:
		for value in ("not-json", ["not", "an", "object"], {"value": "x" * 32769}):
			with (
				self.subTest(value=type(value).__name__),
				self.assertRaises(TransactionalMessageContractError),
			):
				normalize_payload(value)

	def test_snapshot_normalizes_identifiers_reference_and_payload(self) -> None:
		snapshot = normalize_message_snapshot(
			event_id="evt-COD-013-0001",
			event_type="hrp.budget.approved",
			source="hrp-budget",
			destination="integration-bus",
			occurred_at="2026-07-30 09:00:00",
			payload_schema_version=1,
			payload={"b": 2, "a": 1},
			correlation_id="COD-013-snapshot",
			causation_id="evt-COD-013-parent",
			reference_doctype="User",
			reference_name="Administrator",
		)
		self.assertEqual(snapshot["payload_json"], '{"a":1,"b":2}')
		self.assertEqual(snapshot["reference_name"], "Administrator")

		for override in (
			{"event_id": "short"},
			{"event_type": "bad event"},
			{"source": "bad source"},
			{"occurred_at": "not-a-datetime"},
			{"payload_schema_version": 0},
			{"reference_doctype": "User", "reference_name": None},
		):
			values = {
				"event_id": "evt-COD-013-0001",
				"event_type": "hrp.budget.approved",
				"source": "hrp-budget",
				"destination": "integration-bus",
				"occurred_at": "2026-07-30 09:00:00",
				"payload_schema_version": 1,
				"payload": {},
				"correlation_id": "COD-013-snapshot",
				**override,
			}
			with self.subTest(override=override), self.assertRaises(TransactionalMessageContractError):
				normalize_message_snapshot(**values)

	def test_record_names_make_outbox_global_and_inbox_consumer_scoped(self) -> None:
		event_id = "evt-COD-013-record-name"
		outbox = message_record_name("outbox", event_id)
		inbox_a = message_record_name("inbox", event_id, "consumer-a")
		inbox_b = message_record_name("inbox", event_id, "consumer-b")
		self.assertRegex(outbox, r"^out-[0-9a-f]{64}$")
		self.assertRegex(inbox_a, r"^in-[0-9a-f]{64}$")
		self.assertEqual(len(outbox), 68)
		self.assertEqual(len(inbox_a), 67)
		self.assertNotEqual(inbox_a, inbox_b)
		self.assertEqual(inbox_a, message_record_name("inbox", event_id, "consumer-a"))

	def test_processing_token_is_hashed_and_compared_in_constant_time(self) -> None:
		token = "A" * 43
		digest = processing_token_hash(token)
		self.assertRegex(digest, r"^[0-9a-f]{64}$")
		self.assertTrue(processing_token_matches(token, digest))
		self.assertFalse(processing_token_matches("B" * 43, digest))
		with self.assertRaises(TransactionalMessageContractError):
			processing_token_hash("too-short")

	def test_result_and_error_snapshots_are_bounded(self) -> None:
		result = normalize_optional_result({"created": 2})
		self.assertIsNotNone(result)
		self.assertEqual(result[1], '{"created":2}')  # type: ignore[index]
		self.assertIsNone(normalize_optional_result(None))
		self.assertEqual(
			normalize_error_details("REMOTE_TIMEOUT", "Remote endpoint timed out"),
			("REMOTE_TIMEOUT", "Remote endpoint timed out"),
		)
		for values in (
			("bad code", "safe"),
			("REMOTE_TIMEOUT", "line one\nline two"),
			("REMOTE_TIMEOUT", "x" * 501),
		):
			with self.subTest(values=values), self.assertRaises(TransactionalMessageContractError):
				normalize_error_details(*values)

	def test_public_contract_is_read_only_and_discloses_no_roles_or_doctypes(self) -> None:
		contract = get_transactional_message_public_contract()
		self.assertEqual(contract["schema_version"], 1)
		self.assertEqual(len(contract["base_fields"]), len(BASE_MESSAGE_FIELDS))
		self.assertEqual(contract["statuses"]["inbox"], list(INBOX_STATUSES))
		self.assertFalse(contract["http_write_enabled"])
		self.assertFalse(contract["delivery_semantics"]["external_calls_inside_transaction"])
		serialized = str(contract)
		self.assertNotIn("System Manager", serialized)
		self.assertNotIn("HRP Test", serialized)
		payload_hash = next(field for field in BASE_MESSAGE_FIELDS if field.fieldname == "payload_hash")
		self.assertTrue(payload_hash.required)
		self.assertTrue(payload_hash.hidden)

	def test_lifecycle_audit_fields_are_accepted_without_sensitive_markers(self) -> None:
		fields = normalize_audit_fields(
			{
				"attempt_count": 1,
				"box_doctype": "HRP Test Outbox",
				"box_kind": "outbox",
				"box_status": "Processing",
				"error_code": "REMOTE_TIMEOUT",
			}
		)
		self.assertEqual(fields["box_status"], "Processing")


if __name__ == "__main__":
	unittest.main()
