from __future__ import annotations

import unittest

from medops.common.audit_context import (
	AUDIT_CONTEXT_SCHEMA_VERSION,
	AuditContext,
	AuditContextError,
	normalize_audit_event,
	normalize_audit_fields,
	normalize_audit_identifier,
	normalize_correlation_id,
	normalize_logger_name,
	parse_propagation_payload,
)


class TestAuditContext(unittest.TestCase):
	def test_context_has_distinct_public_and_propagation_contracts(self) -> None:
		context = AuditContext(
			correlation_id="COD-010-correlation",
			request_id="req-COD-010-parent",
			channel="http",
			origin="header",
		)

		self.assertEqual(
			context.as_public_dict(),
			{
				"schema_version": AUDIT_CONTEXT_SCHEMA_VERSION,
				"correlation_id": "COD-010-correlation",
				"request_id": "req-COD-010-parent",
				"parent_request_id": None,
				"channel": "http",
			},
		)
		self.assertEqual(
			context.as_propagation_dict(),
			{
				"schema_version": AUDIT_CONTEXT_SCHEMA_VERSION,
				"correlation_id": "COD-010-correlation",
				"parent_request_id": "req-COD-010-parent",
			},
		)
		self.assertNotIn("origin", context.as_public_dict())

	def test_propagated_job_preserves_correlation_and_rotates_request_id(self) -> None:
		context = parse_propagation_payload(
			{
				"schema_version": AUDIT_CONTEXT_SCHEMA_VERSION,
				"correlation_id": "COD-010-correlation",
				"parent_request_id": "req-COD-010-parent",
			},
			request_id="req-COD-010-child",
		)

		self.assertEqual(context.correlation_id, "COD-010-correlation")
		self.assertEqual(context.parent_request_id, "req-COD-010-parent")
		self.assertEqual(context.request_id, "req-COD-010-child")
		self.assertEqual(context.channel, "job")
		self.assertEqual(context.origin, "propagated")

	def test_identifier_contract_is_ascii_bounded_and_not_silently_trimmed(self) -> None:
		self.assertEqual(normalize_correlation_id("A/b:c_d-1.2"), "A/b:c_d-1.2")
		for value in (
			"",
			" leading",
			"trailing ",
			"contains a space",
			"换行",
			"a\nb",
			"../invalid",
			"a" * 141,
			None,
			123,
		):
			with self.subTest(value=value), self.assertRaises(AuditContextError):
				normalize_audit_identifier(value, label="test_id")

	def test_context_rejects_invalid_channel_origin_and_self_parent(self) -> None:
		with self.assertRaisesRegex(AuditContextError, "channel"):
			AuditContext("COD-010-correlation", "req-COD-010-child", "worker")
		with self.assertRaisesRegex(AuditContextError, "origin"):
			AuditContext(
				"COD-010-correlation",
				"req-COD-010-child",
				"job",
				origin="client",
			)
		with self.assertRaisesRegex(AuditContextError, "must differ"):
			AuditContext(
				"COD-010-correlation",
				"req-COD-010-child",
				"job",
				parent_request_id="req-COD-010-child",
			)

	def test_carrier_is_strict_and_versioned(self) -> None:
		for payload in (
			None,
			{},
			{
				"schema_version": 2,
				"correlation_id": "COD-010-correlation",
				"parent_request_id": "req-COD-010-parent",
			},
			{
				"schema_version": 1,
				"correlation_id": "COD-010-correlation",
				"parent_request_id": "req-COD-010-parent",
				"unexpected": True,
			},
		):
			with self.subTest(payload=payload), self.assertRaises(AuditContextError):
				parse_propagation_payload(payload, request_id="req-COD-010-child")

	def test_audit_event_and_logger_names_are_namespaced(self) -> None:
		self.assertEqual(normalize_audit_event("module_enabled_changed"), "module_enabled_changed")
		self.assertEqual(normalize_logger_name("medops.audit"), "medops.audit")
		for value in ("Module Changed", "a", "medops/audit", "frappe.audit"):
			with self.subTest(value=value), self.assertRaises(AuditContextError):
				if value.startswith("ione") or value.startswith("frappe"):
					normalize_logger_name(value)
				else:
					normalize_audit_event(value)

	def test_audit_fields_are_scalar_sorted_and_redacted_by_contract(self) -> None:
		self.assertEqual(
			normalize_audit_fields(
				{
					"changed": True,
					"record_count": 3,
					"module": "HRP Foundation",
				}
			),
			{
				"changed": True,
				"module": "HRP Foundation",
				"record_count": 3,
			},
		)
		for fieldname in (
			"user",
			"user_id",
			"patient_name",
			"request_payload",
			"file_path",
			"api_token",
			"site",
		):
			with self.subTest(fieldname=fieldname), self.assertRaises(AuditContextError):
				normalize_audit_fields({fieldname: "secret"})

	def test_audit_fields_reject_nested_control_and_nonfinite_values(self) -> None:
		for value in (
			{"nested": True},
			["record"],
			"unsafe\nvalue",
			"x" * 161,
			float("inf"),
		):
			with self.subTest(value=value), self.assertRaises(AuditContextError):
				normalize_audit_fields({"safe_field": value})


if __name__ == "__main__":
	unittest.main()
