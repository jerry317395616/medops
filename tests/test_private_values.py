from __future__ import annotations

import json
import unittest
from copy import deepcopy
from hashlib import sha256

from medops.common.domain_service import canonical_json, canonical_json_object, fingerprint_json
from medops.common.private_values import REDACTED_VALUE, PrivateText
from medops.common.transactional_message import normalize_processing_token, processing_token_hash


class TestPrivateValues(unittest.TestCase):
	def test_normalized_snapshots_keep_wire_bytes_values_and_fingerprints(self):
		original = {
			"proof": "public_canary_" + "P" * 32,
			"nested": [{"label": "科室 A", "value": 1}],
			"ok": True,
		}
		normalized, serialized = canonical_json_object(original)
		self.assertEqual(normalized, original)
		self.assertEqual(serialized, canonical_json(original))
		self.assertEqual(json.loads(json.dumps(normalized, ensure_ascii=False)), original)
		self.assertEqual(fingerprint_json(normalized), fingerprint_json(original))
		self.assertEqual(serialized.encode(), canonical_json(original).encode())
		self.assertEqual(
			sha256(serialized.encode()).digest(), sha256(canonical_json(original).encode()).digest()
		)
		for text in (
			repr(normalized),
			repr(serialized),
			repr(deepcopy(normalized)),
			repr(serialized.encode()),
		):
			self.assertNotIn(original["proof"], text)
			self.assertNotIn("科室 A", text)

	def test_string_and_byte_diagnostic_adapters_mask_values(self):
		value = PrivateText("public-canary")
		self.assertIsInstance(value, str)
		self.assertIsInstance(value.encode(), bytes)
		self.assertEqual(value, "public-canary")
		self.assertEqual(value.encode(), b"public-canary")
		self.assertEqual(value.__sentry_repr__(), REDACTED_VALUE)
		self.assertEqual(value.encode().__sentry_repr__(), REDACTED_VALUE)
		self.assertNotIn("public-canary", repr(value))
		self.assertNotIn("public-canary", repr(value.encode()))

	def test_normalized_secret_copies_preserve_schema_scalars_and_detach_mutable_input(self):
		source = {"rows": [{"value": "text", "number": 0, "float": 0.5, "nothing": None}]}
		result, _ = canonical_json_object(source)
		source["rows"][0]["value"] = "changed"
		self.assertEqual(result, {"rows": [{"value": "text", "number": 0, "float": 0.5, "nothing": None}]})

	def test_message_token_hashing_keeps_bytes_and_hides_normalized_nonce(self):
		value = "public_canary_" + "T" * 32
		token = normalize_processing_token(value)
		self.assertEqual(token, value)
		self.assertEqual(processing_token_hash(token), sha256(value.encode()).hexdigest())
		self.assertNotIn(value, repr({"token": token}))
