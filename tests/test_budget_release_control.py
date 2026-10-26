from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime
from typing import cast

from medops.common.budget_ledger import (
	LEDGER,
	LEDGER_PUBLIC_FIELDS,
	RELEASE,
	RELEASE_ENTRY,
	ReleaseExecutionAuthorization,
)
from medops.common.budget_release_control import (
	ReleaseExecutionLease,
	execution_lease,
	expected_release_record,
	release_execution,
	release_mutation,
	release_record,
	release_write,
)
from medops.common.integration_outbox import (
	OutboxRetirement,
	outbox_publication,
	outbox_retirement,
	publication_values,
	retirement_values,
)
from medops.common.transactional_message import message_record_name
from tests.test_budget_ledger import approved_release


def current_record():
	record = release_record()
	assert record is not None
	return record


def current_mutation():
	mutation = release_mutation()
	assert mutation is not None
	return mutation


class TestBudgetReleaseControl(unittest.TestCase):
	def test_terminal_capability_is_bounded_and_does_not_accept_a_secret_or_delivered_state(self):
		value = OutboxRetirement("out-message", "a" * 64, 10, "Processing", "MEDOPS-RELEASE-LEASE-EXHAUSTED")
		for changes in (
			dict(payload_hash="bad"),
			dict(attempt=True),
			dict(attempt=11),
			dict(status="Delivered"),
			dict(reason_code="arbitrary"),
		):
			with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
				with outbox_retirement(replace(value, **changes)):
					self.fail("invalid retirement capability accepted")
		self.assertFalse({"processing_token", "actor", "amount"}.intersection(value.__dataclass_fields__))

	def test_nested_terminal_capability_restores_its_immutable_parent_on_failure(self):
		value = OutboxRetirement(
			"out-message", "a" * 64, 0, "Pending", "MEDOPS-RELEASE-AUTHORIZATION-SUPERSEDED"
		)
		with outbox_retirement(value):
			with self.assertRaises(RuntimeError):
				with outbox_retirement(replace(value, attempt=1, status="Processing")):
					raise RuntimeError("synthetic recovery failure")
			self.assertEqual(retirement_values(), value)
		self.assertIsNone(retirement_values())

	def test_execution_lease_is_ephemeral_restored_and_does_not_repr_the_secret(self):
		authority = ReleaseExecutionAuthorization.from_payload(
			dict(
				release="Release",
				authorization_revision=2,
				intent_digest="a" * 64,
				decision="Decision",
				actor="finance@example.com",
			)
		)
		lease = ReleaseExecutionLease(
			authority,
			message_record_name("outbox", authority.event_id),
			1,
			datetime(2026, 10, 26, 10),
			"S" * 43,
		)
		self.assertNotIn("S" * 43, repr(lease))
		with release_execution(lease):
			self.assertEqual(execution_lease(), lease)
			with self.assertRaises(RuntimeError):
				with release_execution(replace(lease, attempt=2)):
					raise RuntimeError("simulated outer retry")
			self.assertEqual(execution_lease(), lease)
		self.assertIsNone(execution_lease())

	def test_execution_lease_rejects_wrong_message_attempt_and_non_secret_shape(self):
		authority = ReleaseExecutionAuthorization.from_payload(
			dict(
				release="Release",
				authorization_revision=2,
				intent_digest="a" * 64,
				decision="Decision",
				actor="finance@example.com",
			)
		)
		lease = ReleaseExecutionLease(
			authority,
			message_record_name("outbox", authority.event_id),
			1,
			datetime(2026, 10, 26, 10),
			"S" * 43,
		)
		for changes in (
			dict(message="other"),
			dict(attempt=True),
			dict(attempt=0),
			dict(attempt=11),
			dict(processing_token="short"),
		):
			with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
				with release_execution(replace(lease, **changes)):
					self.fail("invalid lease accepted")

	def test_only_declared_operations_and_real_revision_shapes_can_issue_capability(self):
		facts = approved_release()
		for operation, revision in (("post", 0), ("prepare", 1), ("arbitrary", 1), ("confirm", True)):
			with self.subTest(operation=operation, revision=revision), self.assertRaises(ValueError):
				with release_write(operation, facts, revision):
					self.fail("invalid financial capability accepted")
		self.assertIsNone(release_mutation())

	def test_publication_or_preview_cannot_be_used_as_ledger_posting_capability(self):
		with self.assertRaises(ValueError):
			with expected_release_record(LEDGER, {}):
				self.fail("an outer financial authority is required")
		with release_write("preview", approved_release(), 1):
			with self.assertRaises(ValueError):
				with expected_release_record(LEDGER, {}):
					self.fail("a human preview cannot append financial entries")
			with expected_release_record(RELEASE_ENTRY, {"event_kind": "Confirmation"}):
				self.assertEqual(current_record().doctype, RELEASE_ENTRY)

	def test_nested_capabilities_and_exceptions_restore_the_original_boundary(self):
		facts = approved_release()
		with release_write("prepare", facts, 0):
			with self.assertRaises(RuntimeError):
				with (
					release_write("post", facts, 2),
					expected_release_record(LEDGER, {"amount": "1.000000000"}),
				):
					self.assertEqual(current_mutation().operation, "post")
					raise RuntimeError("rollback simulated")
			self.assertEqual(current_mutation().operation, "prepare")
			self.assertIsNone(release_record())
		self.assertIsNone(release_mutation())
		self.assertIsNone(release_record())

	def test_expected_row_is_a_frozen_copy_not_a_mutable_caller_reference(self):
		values = {"identity_key": "original"}
		with release_write("prepare", approved_release(), 0), expected_release_record(RELEASE, values):
			values["identity_key"] = "injected"
			self.assertEqual(current_record().values["identity_key"], "original")
			with self.assertRaises(TypeError):
				cast(dict[str, object], current_record().values)["identity_key"] = "changed"

	def test_outbox_publication_is_complete_immutable_and_does_not_accept_human_secrets(self):
		values = dict(
			event_id="event",
			event_type="medops.budget.authorized",
			source="medops.budget",
			destination="medops.budget.executor",
			payload_hash="a" * 64,
			reference_doctype=RELEASE,
			reference_name="release",
		)
		with outbox_publication(values):
			values["event_id"] = "injected"
			publication = publication_values()
			assert publication is not None
			self.assertEqual(publication["event_id"], "event")
		self.assertIsNone(publication_values())
		for changes in ({"payload_hash": "invalid"}, {"proof": "secret"}, {"event_id": ""}):
			with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
				with outbox_publication(dict(values, **changes)):
					self.fail("invalid owned publication accepted")

	def test_department_projection_has_financial_grain_but_no_private_batch_or_version_proof(self):
		self.assertFalse(
			set(LEDGER_PUBLIC_FIELDS)
			& {
				"voucher_no",
				"voucher_type",
				"release_digest",
				"source_hash",
				"dimensions_json",
				"origin_version_digest",
				"origin_plan_digest",
				"source_snapshot_digest",
				"source_decision_digest",
				"source_event_digest",
			}
		)
		self.assertTrue(
			{
				"amount",
				"currency_precision",
				"organization_unit",
				"cost_center",
				"origin_plan",
				"period_start",
				"period_end",
				"source_submission",
				"source_approval",
			}.issubset(LEDGER_PUBLIC_FIELDS)
		)
