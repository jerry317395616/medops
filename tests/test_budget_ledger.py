from __future__ import annotations

import unittest
from dataclasses import asdict, replace
from decimal import Decimal, localcontext

from medops.common.budget_ledger import (
	BUDGET_LEDGER_DEFINITION,
	FinalBudgetApproval,
	ReleaseExecutionAuthorization,
	allocation_values,
	build_release_preview,
	build_release_request,
	build_reverse_request,
	ledger_entry_key,
	release_facts,
	reversal_facts,
)
from medops.common.budget_workflow import case_identity
from medops.common.domain_service import fingerprint_json
from medops.common.immutable_ledger import normalize_ledger_values
from tests.test_budget_consolidation import approved, combine


def approved_release(*, precision=9):
	snapshot = combine(approved(precision=precision), approved("B", precision=precision))
	approval = FinalBudgetApproval(
		submission=snapshot.identity_key,
		snapshot_digest=snapshot.snapshot_digest,
		approval_case=case_identity(snapshot.identity_key, 1),
		approval_round=1,
		approval_revision=2,
		decision_digest="d" * 64,
		final_review="Consolidated Final Review",
		event_digest="e" * 64,
	)
	return release_facts(
		snapshot,
		approval,
		posting_date="2026-10-26",
		reason="批准年度预算下达",
		prepared_by="finance@example.com",
	)


def reverse(original, **overrides):
	return reversal_facts(
		original,
		**(
			dict(posting_date="2026-10-27", reason="整批冲销预算下达", prepared_by="finance2@example.com")
			| overrides
		),
	)


class TestBudgetLedger(unittest.TestCase):
	def test_release_preview_cannot_change_frozen_financial_intent_or_actor(self):
		value = dict(release="Release", expected_revision=1)
		self.assertEqual(build_release_preview(value).release, "Release")
		for changes in (
			{"expected_revision": True},
			{"expected_revision": 0},
			{"reason": "changed"},
			{"actor": "other"},
			{"amount": "1"},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				build_release_preview(dict(value, **changes))

	def test_execution_authority_contains_only_strict_durable_references(self):
		value = dict(
			release="Release",
			authorization_revision=2,
			intent_digest="a" * 64,
			decision="Decision",
			actor="finance@example.com",
		)
		authority = ReleaseExecutionAuthorization.from_payload(value)
		self.assertEqual(asdict(authority), value)
		for changes in (
			{"authorization_revision": True},
			{"authorization_revision": 1},
			{"intent_digest": "bad"},
			{"actor": ""},
			{"amount": "1"},
			{"method": "arbitrary"},
			{"processing_token": "secret"},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				ReleaseExecutionAuthorization.from_payload(dict(value, **changes))

	def test_execution_event_identity_changes_for_each_authority_epoch(self):
		authority = ReleaseExecutionAuthorization.from_payload(
			dict(
				release="Release",
				authorization_revision=2,
				intent_digest="a" * 64,
				decision="Decision",
				actor="finance@example.com",
			)
		)
		self.assertEqual(
			authority.event_id, ReleaseExecutionAuthorization.from_payload(asdict(authority)).event_id
		)
		for changes in (
			dict(authorization_revision=3),
			dict(decision="New Decision"),
			dict(actor="other@example.com"),
			dict(intent_digest="b" * 64),
		):
			with self.subTest(changes=changes):
				self.assertNotEqual(
					authority.event_id,
					ReleaseExecutionAuthorization.from_payload(dict(asdict(authority), **changes)).event_id,
				)

	def test_financial_request_accepts_references_but_rejects_injected_amount_or_authority(self):
		request = dict(
			approval_case="Approval",
			expected_approval_revision=2,
			posting_date="2026-10-26",
			reason="预算下达",
		)
		self.assertEqual(build_release_request(request).expected_approval_revision, 2)
		for extra in ("amount", "lines", "actor", "operation", "authorized", "source_digest"):
			with self.subTest(extra=extra), self.assertRaises(ValueError):
				build_release_request(dict(request, **{extra: "injected"}))
		for key, value in (
			("reason", ""),
			("reason", "x" * 1001),
			("posting_date", "2026-02-30"),
			("expected_approval_revision", True),
			("expected_approval_revision", 1),
		):
			with self.subTest(key=key, value=value), self.assertRaises(ValueError):
				build_release_request(dict(request, **{key: value}))

	def test_reversal_request_requires_current_revision_and_explicit_reason(self):
		request = dict(release="Release", expected_revision=3, posting_date="2026-10-27", reason="整批冲销")
		self.assertEqual(build_reverse_request(request).expected_revision, 3)
		for changes in (
			{"expected_revision": 0},
			{"expected_revision": True},
			{"reason": ""},
			{"amount": "1"},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				build_reverse_request(dict(request, **changes))

	def test_final_approval_reference_is_bound_to_exact_sealed_consolidation(self):
		original = approved_release()
		self.assertEqual(FinalBudgetApproval.from_payload(asdict(original.approval)), original.approval)
		for changes in (
			{"approval_case": "Other"},
			{"approval_revision": 1},
			{"approval_revision": True},
			{"snapshot_digest": "not-a-digest"},
			{"snapshot_digest": "a" * 64},
		):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				release_facts(
					original.snapshot,
					replace(original.approval, **changes),
					posting_date=original.posting_date,
					reason=original.reason,
					prepared_by=original.prepared_by,
				)

	def test_full_original_grain_and_leaf_provenance_are_kept_without_rollup(self):
		original = approved_release()
		rows = list(allocation_values(original, posting_time="13:14:15.123456"))
		self.assertEqual(len(rows), len(original.snapshot.lines))
		self.assertEqual({row["organization_unit"] for row in rows}, {"UnitA", "UnitB"})
		self.assertEqual({row["cost_center"] for row in rows}, {"CenterA", "CenterB"})
		by_fact = {line.fact_key: line for line in original.snapshot.lines}
		by_plan = {source.origin_plan: source for source in original.snapshot.sources}
		for row in rows:
			line = by_fact[row["fact_key"]]
			for key, value in asdict(line).items():
				self.assertEqual(row[key], value)
			leaf = by_plan[line.origin_plan]
			self.assertEqual(row["source_approval"], leaf.approval_case)
			self.assertEqual(row["source_submission"], leaf.submission)
			self.assertEqual(row["source_event_digest"], leaf.approval_event_digest)
			self.assertNotEqual(row["source_submission"], original.approval.submission)
			self.assertEqual(row["reference_name"], leaf.submission)
			self.assertEqual(row["voucher_no"], original.identity_key)
			self.assertEqual(row["release_digest"], original.intent_digest)

	def test_identity_prevents_reusing_same_approval_with_changed_reason_date_or_actor(self):
		original = approved_release()
		for changes in (
			{"reason": "另一理由"},
			{"posting_date": "2026-10-27"},
			{"prepared_by": "other@example.com"},
		):
			changed = replace(original, **changes)
			self.assertEqual(changed.identity_key, original.identity_key)
			self.assertNotEqual(changed.intent_digest, original.intent_digest)
		approval = replace(
			original.approval, approval_round=2, approval_case=case_identity(original.approval.submission, 2)
		)
		self.assertNotEqual(replace(original, approval=approval).identity_key, original.identity_key)

	def test_whole_reversal_preserves_source_and_binds_original_intent(self):
		original = approved_release()
		reversal = reverse(original)
		self.assertEqual(reversal.snapshot, original.snapshot)
		self.assertEqual(reversal.approval, original.approval)
		self.assertEqual(reversal.total_amount, "-1999999999998.000000002")
		self.assertEqual(reversal.reversal_of, original.identity_key)
		self.assertEqual(reversal.original_intent_digest, original.intent_digest)
		self.assertEqual(reversal.facts()["line_count"], len(original.snapshot.lines))
		self.assertEqual(reverse(original, reason="另一次尝试").identity_key, reversal.identity_key)
		with self.assertRaises(ValueError):
			reverse(reversal)
		with self.assertRaises(ValueError):
			list(allocation_values(reversal, posting_time="13:00:00"))

	def test_allocation_sum_and_exact_profile_survive_every_captured_precision(self):
		for precision in range(10):
			original = approved_release(precision=precision)
			rows = list(allocation_values(original, posting_time="13:00:00"))
			with self.subTest(precision=precision):
				self.assertEqual(sum(Decimal(row["amount"]) for row in rows), Decimal(original.total_amount))
				for row in rows:
					self.assertEqual(row["currency_precision"], precision)
					self.assertEqual(row["amount_schema_version"], 2)
					self.assertIsInstance(row["amount"], str)
					self.assertEqual(Decimal(row["amount"]), Decimal(row["debit"]) - Decimal(row["credit"]))
					self.assertEqual(normalize_ledger_values(row, definition=BUDGET_LEDGER_DEFINITION), row)

	def test_money_and_source_hashes_do_not_depend_on_callers_decimal_context(self):
		original = approved_release()
		rows = list(allocation_values(original, posting_time="13:00:00"))
		with localcontext() as context:
			context.prec = 8
			self.assertEqual(list(allocation_values(original, posting_time="13:00:00")), rows)
			self.assertEqual(reverse(original).total_amount, "-1999999999998.000000002")

	def test_modified_source_or_total_cannot_be_derived_as_approved_allocation(self):
		original = approved_release()
		changed_line = replace(original.snapshot.lines[0], amount="1.000000000")
		for changes in (
			{"total_amount": "1.000000000"},
			{"snapshot": replace(original.snapshot, lines=(changed_line, *original.snapshot.lines[1:]))},
		):
			changed = replace(original, **changes)
			with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
				list(allocation_values(changed, posting_time="13:00:00"))
			with self.assertRaises(ValueError):
				reverse(changed)

	def test_entry_keys_and_hashes_bind_release_original_fact_and_posting_evidence(self):
		original = approved_release()
		rows = list(allocation_values(original, posting_time="13:00:00"))
		self.assertEqual(len({row["entry_key"] for row in rows}), len(rows))
		for row in rows:
			self.assertEqual(row["entry_key"], ledger_entry_key(original.identity_key, row["fact_key"]))
			self.assertEqual(
				row["source_hash"],
				fingerprint_json({key: value for key, value in row.items() if key != "source_hash"}),
			)
		for value in ("bad", "24:00:00", "13:00:00+04:00"):
			with self.subTest(value=value), self.assertRaises(ValueError):
				list(allocation_values(original, posting_time=value))

	def test_invalid_last_source_is_rejected_before_any_allocation_is_yielded(self):
		original = approved_release()
		lines = (*original.snapshot.lines[:-1], replace(original.snapshot.lines[-1], amount="1.000000000"))
		changed = replace(original, snapshot=replace(original.snapshot, lines=lines))
		iterator = allocation_values(changed, posting_time="13:00:00")
		with self.assertRaises(ValueError):
			next(iterator)


if __name__ == "__main__":
	unittest.main()
