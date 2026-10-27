from __future__ import annotations

import json
import math
import unittest
from collections.abc import Iterator

from medops.common.domain_service import (
	DOMAIN_SERVICE_SCHEMA_VERSION,
	DomainServiceContractError,
	DomainServiceDefinition,
	DomainServiceExecution,
	canonical_json,
	canonical_json_object,
	fingerprint_json,
	idempotency_key_hash,
	idempotency_record_name,
	normalize_idempotency_key,
	normalize_service_name,
)


class TestDomainServiceContract(unittest.TestCase):
	def test_definition_is_immutable_and_exposes_no_roles(self) -> None:
		definition = DomainServiceDefinition(
			name="hrp_foundation.module_setting.set_enabled",
			version=1,
			kind="command",
			required_roles=frozenset({"System Manager"}),
		)

		self.assertEqual(
			definition.as_public_dict(),
			{
				"schema_version": DOMAIN_SERVICE_SCHEMA_VERSION,
				"name": "hrp_foundation.module_setting.set_enabled",
				"version": 1,
				"kind": "command",
				"idempotency_required": True,
			},
		)
		self.assertNotIn("roles", definition.as_public_dict())

	def test_definition_rejects_invalid_name_kind_roles_version_and_ttl(self) -> None:
		cases = (
			{"name": "HRP Service"},
			{"kind": "worker"},
			{"required_roles": frozenset()},
			{"required_roles": {"System Manager"}},
			{"required_roles": "System Manager"},
			{"version": 0},
			{"idempotency_ttl_seconds": 10},
		)
		defaults = {
			"name": "hrp_foundation.test.execute",
			"version": 1,
			"kind": "command",
			"required_roles": frozenset({"System Manager"}),
			"idempotency_ttl_seconds": 86400,
		}
		for changes in cases:
			with self.subTest(changes=changes), self.assertRaises(DomainServiceContractError):
				DomainServiceDefinition(**{**defaults, **changes})

	def test_idempotency_key_is_ascii_bounded_and_not_trimmed(self) -> None:
		self.assertEqual(normalize_idempotency_key("COD-011/key:0001"), "COD-011/key:0001")
		for value in (
			None,
			"",
			"short",
			" leading-key",
			"trailing-key ",
			"contains space",
			"中文幂等键",
			"a" * 141,
		):
			with self.subTest(value=value), self.assertRaises(DomainServiceContractError):
				normalize_idempotency_key(value)

	def test_service_name_contract_is_namespaced(self) -> None:
		self.assertEqual(normalize_service_name("hrp_budget.reserve.execute"), "hrp_budget.reserve.execute")
		for value in ("HRP.Service", "ab", "hrp/service", "hrp service", None):
			with self.subTest(value=value), self.assertRaises(DomainServiceContractError):
				normalize_service_name(value)

	def test_canonical_json_is_sorted_recursive_and_deterministic(self) -> None:
		first = {"z": [3, {"b": True, "a": None}], "a": "中国"}
		second = {"a": "中国", "z": [3, {"a": None, "b": True}]}

		self.assertEqual(canonical_json(first), canonical_json(second))
		self.assertEqual(fingerprint_json(first), fingerprint_json(second))
		normalized, serialized = canonical_json_object(first)
		self.assertEqual(normalized["a"], "中国")
		self.assertEqual(serialized, '{"a":"中国","z":[3,{"a":null,"b":true}]}')

	def test_canonical_json_rejects_non_object_unsupported_and_unsafe_values(self) -> None:
		with self.assertRaises(DomainServiceContractError):
			canonical_json_object(["not", "an", "object"])
		for value in (
			{"bad": object()},
			{"bad": math.inf},
			{"bad": b"secret"},
			{"": "empty key"},
			{1: "non-string key"},
			{"bad": "\ud800"},
			{"\ud800": "bad key"},
		):
			with self.subTest(value=value), self.assertRaises(DomainServiceContractError):
				canonical_json(value)

	def test_json_key_control_boundary_preserves_unicode_canonical_bytes(self) -> None:
		class IteratedText(str):
			def __iter__(self) -> Iterator[str]:
				yield "\x01"

		with self.assertRaises(DomainServiceContractError):
			canonical_json({IteratedText("key"): "value"})
		for codepoint in range(32):
			with self.subTest(codepoint=codepoint), self.assertRaises(DomainServiceContractError):
				canonical_json({"key" + chr(codepoint) + "end": "value"})
		for character in (" ", "\x7f", "\x80", "\x9f", "院", "\u0301", "\u200d", "\U0001f3e5"):
			with self.subTest(codepoint=ord(character)):
				payload = {"key" + character + "end": ["原值", "\t\n", True, None]}
				expected = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
				self.assertEqual(canonical_json(payload).encode("utf-8"), expected.encode("utf-8"))

	def test_idempotency_hashes_do_not_contain_the_raw_key(self) -> None:
		deduplication_id = "COD-011-sensitive-key-0001"
		key_hash = idempotency_key_hash(deduplication_id)
		record_name = idempotency_record_name(
			"hrp_foundation.test.execute",
			deduplication_id,
		)

		self.assertRegex(key_hash, r"^[0-9a-f]{64}$")
		self.assertRegex(record_name, r"^idp-[0-9a-f]{64}$")
		self.assertNotIn(deduplication_id, key_hash)
		self.assertNotIn(deduplication_id, record_name)

	def test_execution_public_contract_keeps_context_outside_result_snapshot(self) -> None:
		execution = DomainServiceExecution(
			service="hrp_foundation.test.execute",
			service_version=1,
			result={"changed": True},
			correlation_id="COD-011-correlation",
			request_id="req-COD-011-request",
			idempotency_replayed=False,
		)

		self.assertEqual(
			execution.as_public_dict(),
			{
				"service": "hrp_foundation.test.execute",
				"service_version": 1,
				"result": {"changed": True},
				"correlation_id": "COD-011-correlation",
				"request_id": "req-COD-011-request",
				"idempotency_replayed": False,
			},
		)


if __name__ == "__main__":
	unittest.main()
