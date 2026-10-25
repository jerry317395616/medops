from __future__ import annotations

import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from medops.common.error_catalog import (
	ErrorCatalogError,
	IoneApplicationError,
	load_error_catalog,
	load_translation_map,
	parse_error_catalog,
	validate_error_translations,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "medops/config/error_catalog.json"
TRANSLATION_PATH = ROOT / "medops/translations/zh.csv"


class TestErrorCatalog(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
		cls.catalog = load_error_catalog(CATALOG_PATH)

	def _parse(self, mutate) -> None:
		payload = copy.deepcopy(self.payload)
		mutate(payload)
		parse_error_catalog(payload)

	def test_current_catalog_is_complete_translated_and_deterministic(self) -> None:
		repeated = load_error_catalog(CATALOG_PATH)
		translations = validate_error_translations(self.catalog, TRANSLATION_PATH)

		self.assertEqual(len(self.catalog.errors), 12)
		self.assertEqual(self.catalog.sha256, repeated.sha256)
		self.assertRegex(self.catalog.sha256, r"^[0-9a-f]{64}$")
		self.assertEqual(translations["Authentication is required."], "需要先登录。")

	def test_lookup_supports_symbolic_key_and_stable_code(self) -> None:
		by_key = self.catalog.get("AUTHENTICATION_REQUIRED")
		by_code = self.catalog.get("IONE-CORE-0001")

		self.assertIs(by_key, by_code)
		self.assertEqual(by_key.http_status, 401)
		self.assertFalse(by_key.retryable)
		with self.assertRaisesRegex(ErrorCatalogError, "Unknown I-ONE error"):
			self.catalog.get("UNKNOWN")

	def test_public_views_exclude_internal_logging_metadata(self) -> None:
		catalog_view = self.catalog.as_public_dict()
		definition_view = cast(list[dict[str, object]], catalog_view["errors"])[0]
		error = IoneApplicationError(
			self.catalog.get("INVALID_REQUEST"),
			"COD-009-error-id",
			public_message="请求参数无效。",
		)

		self.assertNotIn("log_level", definition_view)
		self.assertNotIn("key", definition_view)
		self.assertNotIn("app", catalog_view)
		self.assertEqual(
			error.as_public_dict(),
			{
				"schema_version": 1,
				"code": "IONE-CORE-0003",
				"category": "validation",
				"message": "请求参数无效。",
				"error_id": "COD-009-error-id",
				"retryable": False,
			},
		)
		self.assertNotIn("key", error.as_public_dict())
		self.assertNotIn("http_status", error.as_public_dict())

	def test_root_and_definition_keys_are_strict(self) -> None:
		with self.assertRaisesRegex(ErrorCatalogError, "root must be an object"):
			parse_error_catalog([])
		with self.assertRaisesRegex(ErrorCatalogError, "keys mismatch"):
			self._parse(lambda payload: payload.update({"unexpected": True}))
		with self.assertRaisesRegex(ErrorCatalogError, "keys mismatch"):
			self._parse(lambda payload: payload["errors"][0].update({"unexpected": True}))

	def test_duplicate_keys_codes_and_messages_are_rejected(self) -> None:
		for field in ("key", "code", "message"):
			with (
				self.subTest(field=field),
				self.assertRaisesRegex(
					ErrorCatalogError,
					"duplicate|must be",
				),
			):
				self._parse(
					lambda payload, field=field: payload["errors"][1].update(
						{field: payload["errors"][0][field]}
					)
				)

	def test_codes_must_be_contiguous_and_in_order(self) -> None:
		with self.assertRaisesRegex(ErrorCatalogError, "must be IONE-CORE-0002"):
			self._parse(lambda payload: payload["errors"][1].update({"code": "IONE-CORE-0099"}))
		with self.assertRaisesRegex(ErrorCatalogError, "namespace must be IONE-CORE"):
			self._parse(lambda payload: payload.update({"namespace": "OTHER"}))

	def test_category_status_retry_and_log_level_are_consistent(self) -> None:
		with self.assertRaisesRegex(ErrorCatalogError, "http_status is invalid"):
			self._parse(lambda payload: payload["errors"][2].update({"http_status": 500}))
		with self.assertRaisesRegex(ErrorCatalogError, "retryable is inconsistent"):
			self._parse(lambda payload: payload["errors"][9].update({"retryable": False}))
		with self.assertRaisesRegex(ErrorCatalogError, "log_level must be error"):
			self._parse(lambda payload: payload["errors"][8].update({"log_level": "warning"}))

	def test_public_messages_reject_dynamic_or_unsafe_content(self) -> None:
		for message in (
			"Invalid field {field}.",
			"<b>Invalid request.</b>",
			"Invalid request.\nTry again.",
			"请求无效。",
		):
			with (
				self.subTest(message=message),
				self.assertRaisesRegex(
					ErrorCatalogError,
					"static, single-line English sentence",
				),
			):
				self._parse(
					lambda payload, message=message: payload["errors"][2].update({"message": message})
				)

	def test_translation_file_is_strict_and_must_cover_catalog(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "zh.csv"
			with path.open("w", encoding="utf-8", newline="") as target:
				writer = csv.writer(target, lineterminator="\n")
				writer.writerow(["Authentication is required.", "需要先登录。", ""])

			with self.assertRaisesRegex(ErrorCatalogError, "translations are missing"):
				validate_error_translations(self.catalog, path)

			with path.open("a", encoding="utf-8", newline="") as target:
				writer = csv.writer(target, lineterminator="\n")
				writer.writerow(["Authentication is required.", "重复。", ""])
			with self.assertRaisesRegex(ErrorCatalogError, "duplicate source"):
				load_translation_map(path)

	def test_application_error_rejects_invalid_error_id(self) -> None:
		with self.assertRaisesRegex(ErrorCatalogError, "error_id is invalid"):
			IoneApplicationError(self.catalog.get("INTERNAL_ERROR"), "../secret")


if __name__ == "__main__":
	unittest.main()
