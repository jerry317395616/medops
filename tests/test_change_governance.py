from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from medops.common.change_governance import (
	ChangedPath,
	ChangeGovernanceError,
	GovernanceReport,
	assess_changed_paths,
	inspect_change_governance,
	load_change_policy,
	parse_adr_text,
	parse_change_record,
	validate_adr_transition,
)

ROOT = Path(__file__).resolve().parents[1]


class TestChangeGovernance(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.report = inspect_change_governance(ROOT)

	def _changed_paths(self, *extra: ChangedPath) -> tuple[ChangedPath, ...]:
		return (
			ChangedPath("M", "changes/COD-008.json"),
			ChangedPath("M", "backlog/COD-008.md"),
			ChangedPath("M", "backlog/backlog.csv"),
			ChangedPath("M", "pyproject.toml"),
			*extra,
		)

	def _replace_cod008(self, **changes: object) -> GovernanceReport:
		record = replace(self.report.change_by_task["COD-008"], **changes)
		records = tuple(record if item.task_id == record.task_id else item for item in self.report.changes)
		return replace(self.report, changes=records)

	def test_current_repository_is_complete_and_deterministic(self) -> None:
		repeated = inspect_change_governance(ROOT)

		self.assertEqual(len(self.report.tasks), 118)
		self.assertEqual(len(self.report.changes), 37)
		self.assertEqual(len(self.report.decisions), 36)
		self.assertEqual(self.report.sha256, repeated.sha256)
		self.assertRegex(self.report.sha256, r"^[0-9a-f]{64}$")

	def test_public_report_is_redacted(self) -> None:
		serialized = json.dumps(self.report.as_public_dict(), ensure_ascii=False)

		self.assertNotIn(str(ROOT), serialized)
		self.assertNotIn("architecture/adr", serialized)
		self.assertNotIn("产品负责人", serialized)
		self.assertNotIn("背景与问题", serialized)
		self.assertIn("ADR-0002", serialized)
		self.assertIn("ADR-0004", serialized)

	def test_policy_rejects_unknown_fields_and_invalid_risk_order(self) -> None:
		payload = json.loads((ROOT / "medops/config/change_governance.json").read_text(encoding="utf-8"))
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "policy.json"
			payload["unexpected"] = True
			path.write_text(json.dumps(payload), encoding="utf-8")
			with self.assertRaisesRegex(ChangeGovernanceError, "keys mismatch"):
				load_change_policy(path)

			del payload["unexpected"]
			payload["allowed_risk_levels"] = ["low", "high", "medium", "critical"]
			path.write_text(json.dumps(payload), encoding="utf-8")
			with self.assertRaisesRegex(ChangeGovernanceError, "low, medium, high, critical"):
				load_change_policy(path)

	def test_adr_parser_rejects_unknown_fields_and_missing_sections(self) -> None:
		path = ROOT / "architecture/adr/ADR-0002-source-controlled-change-governance.md"
		text = path.read_text(encoding="utf-8")

		with self.assertRaisesRegex(ChangeGovernanceError, "keys mismatch"):
			parse_adr_text(
				text.replace("superseded_by:\n", "superseded_by:\nunknown: true\n", 1),
				path.name,
				self.report.policy,
			)
		with self.assertRaisesRegex(ChangeGovernanceError, "sections"):
			parse_adr_text(
				text.replace("## 安全与合规", "## 其他"),
				path.name,
				self.report.policy,
			)

	def test_adr_requires_two_real_alternatives(self) -> None:
		path = ROOT / "architecture/adr/ADR-0002-source-controlled-change-governance.md"
		text = path.read_text(encoding="utf-8")
		text = text.replace(
			"2. 仅使用自由格式 Markdown",
			"- 仅使用自由格式 Markdown",
			1,
		).replace(
			"3. Git 中使用严格 ADR",
			"- Git 中使用严格 ADR",
			1,
		)

		with self.assertRaisesRegex(ChangeGovernanceError, "at least two alternatives"):
			parse_adr_text(text, path.name, self.report.policy)

	def test_change_record_rejects_unknown_module_and_unknown_field(self) -> None:
		path = ROOT / "changes/COD-008.json"
		payload = json.loads(path.read_text(encoding="utf-8"))
		modules = frozenset(
			line.strip()
			for line in (ROOT / "medops/modules.txt").read_text(encoding="utf-8").splitlines()
			if line.strip()
		)
		payload["affected_modules"] = ["HRP Not Registered"]
		with self.assertRaisesRegex(ChangeGovernanceError, "undeclared module"):
			parse_change_record(payload, path.name, self.report.policy, modules)

		payload["affected_modules"] = ["HRP Foundation"]
		payload["unknown"] = True
		with self.assertRaisesRegex(ChangeGovernanceError, "keys mismatch"):
			parse_change_record(payload, path.name, self.report.policy, modules)

	def test_assessment_is_idempotent_and_raises_risk(self) -> None:
		first = assess_changed_paths(
			self.report,
			self._changed_paths(),
			task_id="COD-008",
		)
		second = assess_changed_paths(
			self.report,
			self._changed_paths(),
			task_id="COD-008",
		)

		self.assertEqual(first.sha256, second.sha256)
		self.assertEqual(first.required_risk, "critical")
		self.assertIn("application-boundary", first.matched_rule_ids)
		self.assertEqual(first.accepted_adr_ids, ("ADR-0002",))

	def test_required_task_metadata_is_risk_neutral_but_still_hashed(self) -> None:
		metadata = (
			ChangedPath("A", "changes/COD-009.json"),
			ChangedPath("A", "backlog/COD-009.md"),
			ChangedPath("M", "backlog/backlog.csv"),
		)

		assessment = assess_changed_paths(self.report, metadata, task_id="COD-009")

		self.assertEqual(assessment.required_risk, "low")
		self.assertEqual(assessment.matched_rule_ids, ())
		self.assertEqual(len(assessment.changed_paths), 3)
		self.assertRegex(assessment.sha256, r"^[0-9a-f]{64}$")

	def test_substantive_governance_path_still_requires_high_risk_accepted_adr(self) -> None:
		assessment = assess_changed_paths(
			self.report,
			(
				ChangedPath("A", "changes/COD-009.json"),
				ChangedPath("A", "backlog/COD-009.md"),
				ChangedPath("M", "backlog/backlog.csv"),
				ChangedPath("M", "architecture/change_governance.md"),
			),
			task_id="COD-009",
		)

		self.assertEqual(assessment.required_risk, "high")
		self.assertIn("governance-control", assessment.matched_rule_ids)
		self.assertEqual(assessment.accepted_adr_ids, ("ADR-0003", "ADR-0004"))

	def test_assessment_rejects_missing_governance_files_and_uncovered_paths(self) -> None:
		with self.assertRaisesRegex(ChangeGovernanceError, "missing required governance files"):
			assess_changed_paths(
				self.report,
				(
					ChangedPath("M", "changes/COD-008.json"),
					ChangedPath("M", "pyproject.toml"),
				),
				task_id="COD-008",
			)

		with self.assertRaisesRegex(ChangeGovernanceError, "does not cover paths"):
			assess_changed_paths(
				self.report,
				self._changed_paths(ChangedPath("A", "unapproved-root-file.txt")),
				task_id="COD-008",
			)

	def test_assessment_rejects_risk_understatement_and_missing_accepted_adr(self) -> None:
		with self.assertRaisesRegex(ChangeGovernanceError, "require critical"):
			assess_changed_paths(
				self._replace_cod008(risk_level="medium"),
				self._changed_paths(),
				task_id="COD-008",
			)
		with self.assertRaisesRegex(ChangeGovernanceError, "Accepted ADR"):
			assess_changed_paths(
				self._replace_cod008(adr_ids=()),
				self._changed_paths(),
				task_id="COD-008",
			)

	def test_assessment_rejects_adr_deletion(self) -> None:
		with self.assertRaisesRegex(ChangeGovernanceError, "cannot be deleted"):
			assess_changed_paths(
				self.report,
				self._changed_paths(
					ChangedPath(
						"D",
						"architecture/adr/ADR-0002-source-controlled-change-governance.md",
					)
				),
				task_id="COD-008",
			)

	def test_baseline_task_may_backfill_completed_change_records_once(self) -> None:
		assessment = assess_changed_paths(
			self.report,
			(
				ChangedPath("A", "changes/COD-001.json"),
				ChangedPath("A", "changes/COD-008.json"),
				ChangedPath("M", "backlog/COD-008.md"),
				ChangedPath("M", "backlog/backlog.csv"),
				ChangedPath("M", "pyproject.toml"),
			),
		)
		self.assertEqual(assessment.task_id, "COD-008")

		with self.assertRaisesRegex(ChangeGovernanceError, "exactly one"):
			assess_changed_paths(
				self.report,
				(
					ChangedPath("M", "changes/COD-007.json"),
					ChangedPath("M", "changes/COD-008.json"),
					ChangedPath("M", "backlog/COD-008.md"),
					ChangedPath("M", "backlog/backlog.csv"),
				),
			)

	def test_accepted_adr_is_immutable_and_transition_machine_is_closed(self) -> None:
		accepted = self.report.decision_by_id["ADR-0001"]
		decisions = dict(self.report.decision_by_id)

		with self.assertRaisesRegex(ChangeGovernanceError, "immutable"):
			validate_adr_transition(
				accepted,
				replace(accepted, body=accepted.body + "\nChanged.\n"),
				decisions,
				self.report.policy,
			)
		with self.assertRaisesRegex(ChangeGovernanceError, "cannot transition"):
			validate_adr_transition(
				accepted,
				replace(accepted, status="Rejected"),
				decisions,
				self.report.policy,
			)

	def test_proposed_can_be_accepted_and_accepted_can_be_superseded(self) -> None:
		accepted = replace(self.report.decision_by_id["ADR-0001"], status="Accepted", superseded_by=None)
		successor = replace(
			self.report.decision_by_id["ADR-0002"],
			supersedes=("ADR-0001",),
		)
		superseded = replace(
			accepted,
			status="Superseded",
			superseded_by="ADR-0002",
		)

		validate_adr_transition(
			replace(accepted, status="Proposed"),
			accepted,
			dict(self.report.decision_by_id),
			self.report.policy,
		)
		validate_adr_transition(
			accepted,
			superseded,
			{"ADR-0001": superseded, "ADR-0002": successor},
			self.report.policy,
		)


if __name__ == "__main__":
	unittest.main()
