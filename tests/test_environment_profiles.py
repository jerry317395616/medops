from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from medops.common.environment_profiles import (
	EnvironmentProfileError,
	load_environment_registry,
	parse_environment_registry,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "medops" / "config" / "environment_profiles.json"


class EnvironmentProfileTest(unittest.TestCase):
	def _payload(self) -> dict[str, Any]:
		return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))

	def test_current_registry_is_complete_and_isolated(self) -> None:
		registry = load_environment_registry()
		self.assertEqual(
			[profile.name for profile in registry.profiles],
			["development", "test", "demo"],
		)
		self.assertEqual(registry.required_apps, ("frappe", "erpnext", "hrms", "medops"))
		self.assertEqual(
			len({port for profile in registry.profiles for port in profile.ports.values()}),
			15,
		)

	def test_public_profile_does_not_expose_paths_or_ports(self) -> None:
		registry = load_environment_registry()
		public_profile = registry.get("demo").as_public_dict(registry.schema_version)
		self.assertNotIn("bench_dir", public_profile)
		self.assertNotIn("site_name", public_profile)
		self.assertNotIn("ports", public_profile)
		self.assertTrue(public_profile["synthetic_data_only"])
		self.assertFalse(public_profile["external_integrations_enabled"])

	def test_rejects_external_integrations_in_nonproduction(self) -> None:
		payload = self._payload()
		payload["profiles"]["demo"]["external_integrations_enabled"] = True
		with self.assertRaisesRegex(EnvironmentProfileError, "external integrations"):
			parse_environment_registry(payload)

	def test_rejects_duplicate_service_ports(self) -> None:
		payload = self._payload()
		payload["profiles"]["demo"]["ports"]["webserver"] = payload["profiles"]["test"]["ports"]["webserver"]
		with self.assertRaisesRegex(EnvironmentProfileError, "ports must be unique"):
			parse_environment_registry(payload)

	def test_rejects_unknown_profile_keys(self) -> None:
		payload = copy.deepcopy(self._payload())
		payload["profiles"]["test"]["password"] = "must-not-exist"
		with self.assertRaisesRegex(EnvironmentProfileError, "must contain exactly"):
			parse_environment_registry(payload)

	def test_load_failure_does_not_fall_back_to_defaults(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			path = Path(temp) / "profiles.json"
			path.write_text("{", encoding="utf-8")
			with self.assertRaisesRegex(EnvironmentProfileError, "Cannot load"):
				load_environment_registry(path)


if __name__ == "__main__":
	unittest.main()
