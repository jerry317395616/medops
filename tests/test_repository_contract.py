from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.repository_contract import (
	APP_NAME,
	collect_violations,
	discover_custom_apps,
	find_direct_audit_loggers,
	find_direct_frappe_throws,
	find_direct_immutable_ledger_bypasses,
	find_direct_transaction_commits,
	find_direct_transactional_message_bypasses,
	find_legacy_prefix_references,
	validate_audit_context_contract,
	validate_branch_policy,
	validate_change_governance,
	validate_ci_pipeline,
	validate_core_reports_contract,
	validate_data_quality_contract,
	validate_domain_service_contract,
	validate_environment_profiles,
	validate_error_contract,
	validate_external_code_mapping_contract,
	validate_fixture_governance,
	validate_immutable_ledger_contract,
	validate_master_data_governance_contract,
	validate_module_boundaries,
	validate_numbering_contract,
	validate_organization_hierarchy_contract,
	validate_organization_mapping_contract,
	validate_performance_baseline_contract,
	validate_push_guard,
	validate_quality_tooling,
	validate_segregation_contract,
	validate_system_settings_contract,
	validate_test_data_factory_contract,
	validate_transactional_message_contract,
)

ROOT = Path(__file__).resolve().parents[1]


class RepositoryContractTest(unittest.TestCase):
	def test_prepared_core_report_is_rejected_by_contract(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			path = Path(
				"medops/hrp_master_data/report/hrp_data_quality_issue_trend/hrp_data_quality_issue_trend.json"
			)
			metadata = json.loads((ROOT / path).read_text(encoding="utf-8"))
			metadata["prepared_report"] = 1
			(root / path).parent.mkdir(parents=True)
			(root / path).write_text(json.dumps(metadata), encoding="utf-8")
			self.assertIn(
				"core report must execute live reviewed source: RPT-006", validate_core_reports_contract(root)
			)

	def test_current_repository_satisfies_contract(self) -> None:
		self.assertEqual(collect_violations(ROOT), [])

	def test_current_app_is_press_discoverable_from_repository_root(self) -> None:
		self.assertEqual(discover_custom_apps(ROOT)[APP_NAME], ROOT)
		self.assertTrue((ROOT / APP_NAME / "hooks.py").is_file())

	def test_discovers_multiple_custom_apps(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			for name in (APP_NAME, "medops_budget"):
				app_root = root / "apps" / name
				app_root.mkdir(parents=True)
				(app_root / "pyproject.toml").write_text(
					f'[project]\nname = "{name}"\nversion = "1.0.0"\n',
					encoding="utf-8",
				)
			self.assertEqual(set(discover_custom_apps(root)), {APP_NAME, "medops_budget"})

	def test_finds_legacy_prefix(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			legacy_prefix = "myi" + "_hrp"
			(root / "README.md").write_text(f"legacy app: {legacy_prefix}", encoding="utf-8")
			self.assertEqual(
				find_legacy_prefix_references(root),
				[f"README.md contains {legacy_prefix}"],
			)

	def test_rejects_weakened_branch_policy(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			policy_path = root / ".github" / "branch-protection.json"
			policy_path.parent.mkdir(parents=True)
			policy_path.write_text(
				json.dumps(
					{
						"default_branch": "main",
						"protection": {
							"required_status_checks": {
								"strict": False,
								"contexts": [],
							},
							"enforce_admins": False,
							"required_pull_request_reviews": None,
							"allow_force_pushes": True,
							"allow_deletions": True,
						},
					}
				),
				encoding="utf-8",
			)
			violations = validate_branch_policy(root)
			self.assertIn("pull requests must be required before merging", violations)
			self.assertIn("required CI checks must use the latest main branch", violations)
			self.assertIn("force pushes must be disabled", violations)

	def test_solo_maintainer_policy_still_requires_pull_requests(self) -> None:
		policy = json.loads((ROOT / ".github" / "branch-protection.json").read_text(encoding="utf-8"))
		reviews = policy["protection"]["required_pull_request_reviews"]
		self.assertIsInstance(reviews, dict)
		self.assertEqual(reviews["required_approving_review_count"], 0)
		self.assertFalse(reviews["require_code_owner_reviews"])

	def test_rejects_missing_push_guard(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			self.assertEqual(
				validate_push_guard(root),
				["missing .githooks/pre-push"],
			)

	def test_rejects_private_cross_module_import(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			source = root / "medops" / "hrp_budget" / "services" / "budget.py"
			source.parent.mkdir(parents=True)
			source.write_text(
				"from medops.hrp_procurement.doctype.purchase import Purchase\n",
				encoding="utf-8",
			)
			violations = validate_module_boundaries(root)
			self.assertEqual(len(violations), 1)
			self.assertIn("imports private cross-module path", violations[0])

	def test_allows_cross_module_service_facade(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			source = root / "medops" / "hrp_budget" / "services" / "budget.py"
			source.parent.mkdir(parents=True)
			source.write_text(
				"from medops.hrp_procurement.services.public import submit_request\n",
				encoding="utf-8",
			)
			self.assertEqual(validate_module_boundaries(root), [])

	def test_current_quality_tooling_is_mandatory(self) -> None:
		self.assertEqual(validate_quality_tooling(ROOT), [])

	def test_current_ci_pipeline_is_mandatory(self) -> None:
		self.assertEqual(validate_ci_pipeline(ROOT), [])

	def test_current_environment_profiles_are_mandatory(self) -> None:
		self.assertEqual(validate_environment_profiles(ROOT), [])

	def test_current_fixture_governance_is_mandatory(self) -> None:
		self.assertEqual(validate_fixture_governance(ROOT), [])

	def test_current_change_governance_is_mandatory(self) -> None:
		self.assertEqual(validate_change_governance(ROOT), [])

	def test_current_error_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_error_contract(ROOT), [])

	def test_current_audit_context_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_audit_context_contract(ROOT), [])

	def test_current_domain_service_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_domain_service_contract(ROOT), [])

	def test_current_immutable_ledger_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_immutable_ledger_contract(ROOT), [])

	def test_current_transactional_message_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_transactional_message_contract(ROOT), [])

	def test_current_test_data_factory_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_test_data_factory_contract(ROOT), [])

	def test_current_performance_baseline_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_performance_baseline_contract(ROOT), [])

	def test_current_system_settings_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_system_settings_contract(ROOT), [])

	def test_current_organization_hierarchy_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_organization_hierarchy_contract(ROOT), [])

	def test_current_organization_mapping_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_organization_mapping_contract(ROOT), [])

	def test_current_master_data_governance_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_master_data_governance_contract(ROOT), [])

	def test_current_external_code_mapping_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_external_code_mapping_contract(ROOT), [])

	def test_current_data_quality_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_data_quality_contract(ROOT), [])

	def test_current_numbering_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_numbering_contract(ROOT), [])

	def test_current_segregation_contract_is_mandatory(self) -> None:
		self.assertEqual(validate_segregation_contract(ROOT), [])

	def test_rejects_direct_transaction_commit(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			source = root / "medops" / "hrp_budget" / "services" / "unsafe.py"
			source.parent.mkdir(parents=True)
			source.write_text(
				"import frappe\n\n\ndef unsafe():\n\tfrappe.db.commit()\n",
				encoding="utf-8",
			)

			violations = find_direct_transaction_commits(root)

		self.assertEqual(len(violations), 1)
		self.assertIn("the outer Frappe transaction owns the commit", violations[0])

	def test_rejects_local_transaction_commit_in_services_and_hooks(self) -> None:
		for relative in ("medops/services/unsafe.py", "medops/hooks.py"):
			with self.subTest(path=relative), tempfile.TemporaryDirectory() as temp:
				root = Path(temp)
				source = root / relative
				source.parent.mkdir(parents=True, exist_ok=True)
				source.write_text(
					"import frappe\n\ndef unsafe():\n\tfrappe.local.db.commit()\n", encoding="utf-8"
				)
				violations = find_direct_transaction_commits(root)
				self.assertEqual(len(violations), 1)
				self.assertIn("calls frappe.local.db.commit", violations[0])

	def test_native_fixture_commit_exception_requires_a_real_test_path(self) -> None:
		for relative, accessor, allowed in (
			("medops/hrp_budget/tests/test_fixture.py", "frappe.local.db", True),
			("medops/services/test_fixture.py", "frappe.local.db", False),
			("medops/hrp_budget/tests/service.py", "frappe.local.db", False),
			("medops/hrp_budget/tests/test_fixture.py", "frappe.db", False),
		):
			with self.subTest(path=relative, accessor=accessor), tempfile.TemporaryDirectory() as temp:
				root = Path(temp)
				source = root / relative
				source.parent.mkdir(parents=True, exist_ok=True)
				source.write_text(
					f"import frappe\n\ndef fixture():\n\t{accessor}.commit()\n", encoding="utf-8"
				)
				self.assertEqual(len(find_direct_transaction_commits(root)), 0 if allowed else 1)

	def test_rejects_direct_audit_logger_outside_context_service(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			source = root / "medops" / "services" / "unsafe.py"
			source.parent.mkdir(parents=True)
			source.write_text(
				'import frappe\n\n\ndef unsafe():\n\tfrappe.logger("medops.unsafe").info({"user": "x"})\n',
				encoding="utf-8",
			)

			violations = find_direct_audit_loggers(root)

		self.assertEqual(len(violations), 1)
		self.assertIn("calls frappe.logger outside", violations[0])

	def test_rejects_direct_frappe_throw_outside_error_service(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			source = root / "medops" / "api" / "v1" / "unsafe.py"
			source.parent.mkdir(parents=True)
			source.write_text(
				'import frappe\n\n\ndef unsafe():\n\tfrappe.throw("raw payload")\n',
				encoding="utf-8",
			)

			violations = find_direct_frappe_throws(root)

		self.assertEqual(len(violations), 1)
		self.assertIn("calls frappe.throw outside", violations[0])

	def test_rejects_direct_frappe_only_for_outside_error_service(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			source = root / "medops" / "services" / "unsafe.py"
			source.parent.mkdir(parents=True)
			source.write_text(
				'import frappe\n\n\ndef unsafe():\n\tfrappe.only_for("System Manager")\n',
				encoding="utf-8",
			)

			violations = find_direct_frappe_throws(root)

		self.assertEqual(len(violations), 1)
		self.assertIn("calls frappe.only_for outside", violations[0])

	def test_rejects_direct_immutable_ledger_bypass(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			source = root / "medops" / "hrp_budget" / "services" / "unsafe.py"
			source.parent.mkdir(parents=True)
			source.write_text(
				'import frappe\n\n\ndef unsafe():\n\treturn frappe.get_doc("HRP Budget Ledger", "x")\n',
				encoding="utf-8",
			)

			violations = find_direct_immutable_ledger_bypasses(root)

		self.assertEqual(len(violations), 1)
		self.assertIn("bypasses the immutable-ledger service", violations[0])

	def test_rejects_direct_transactional_message_bypass(self) -> None:
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			source = root / "medops" / "hrp_budget" / "services" / "unsafe.py"
			source.parent.mkdir(parents=True)
			source.write_text(
				'import frappe\n\n\ndef unsafe():\n\treturn frappe.get_doc("HRP Budget Outbox", "x")\n',
				encoding="utf-8",
			)

			violations = find_direct_transactional_message_bypasses(root)

		self.assertEqual(len(violations), 1)
		self.assertIn("bypasses the transactional-message service", violations[0])


if __name__ == "__main__":
	unittest.main()
