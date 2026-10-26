from __future__ import annotations

import unittest
from copy import deepcopy
from dataclasses import replace

from medops.common.budget_consolidation import (
	ApprovedDepartment,
	build_consolidation,
	consolidate_departments,
	consolidated_snapshot_from_payload,
)
from medops.common.budget_workflow import case_identity
from tests.test_budget_submission import snapshot, source


def approved(label="A", *, precision=9, unit=None, center=None, period=1):
	plan, version = source(precision)
	plan.update(
		name="Plan" + label,
		budget_version="Version" + label,
		organization_unit=unit or "Unit" + label,
		cost_center=center or "Center" + label,
	)
	version["name"] = plan["budget_version"]
	if period == 2:
		plan["lines"][0].update(period_index=2, period_start="2026-02-01", period_end="2026-02-28")
	leaf = snapshot(plan, version)
	ref = ApprovedDepartment(
		submission=leaf.identity_key,
		snapshot_digest=leaf.snapshot_digest,
		approval_case=case_identity(leaf.identity_key, 1),
		approval_round=1,
		approval_revision=2,
		approval_decision_digest="b" * 64,
		approval_final_review="Review" + label,
		approval_event_digest="c" * 64,
		origin_plan=leaf.source_plan,
		preparer="maker" + label + "@example.com",
		applicant=leaf.applicant,
		requested_by="requester" + label + "@example.com",
	)
	return ref, leaf


def combine(*items):
	return consolidate_departments(
		items or (approved(), approved("B")),
		organization_unit="Root",
		subject="医院年度汇总",
		applicant="manager@example.com",
		posting_date="2026-10-26",
	)


class TestBudgetConsolidation(unittest.TestCase):
	def test_large_exact_total_and_full_leaf_grain_round_trip(self):
		item = combine()
		self.assertEqual(item.total_amount, "1999999999998.000000002")
		self.assertIsNone(item.cost_center)
		self.assertEqual({row.organization_unit for row in item.lines}, {"UnitA", "UnitB"})
		self.assertEqual({row.cost_center for row in item.lines}, {"CenterA", "CenterB"})
		self.assertEqual(consolidated_snapshot_from_payload(item.as_dict()), item)

	def test_compatible_versions_and_all_captured_precisions_are_supported(self):
		for precision in range(10):
			with self.subTest(precision=precision):
				item = combine(approved(precision=precision), approved("B", precision=precision))
				self.assertEqual(item.context.currency_precision, precision)
				self.assertEqual({row.origin_version for row in item.lines}, {"VersionA", "VersionB"})

	def test_permutation_is_canonical_but_new_subject_does_not_duplicate_source_identity(self):
		a, b = approved(), approved("B")
		left, right = combine(a, b), combine(b, a)
		self.assertEqual(left, right)
		changed = replace(left, subject="另一汇总说明")
		self.assertEqual(left.identity_key, changed.identity_key)
		self.assertNotEqual(left.snapshot_digest, changed.snapshot_digest)

	def test_different_tenant_organization_year_currency_or_precision_are_rejected(self):
		ref, leaf = approved("B")
		for field, value in (
			("company", "Other"),
			("hospital", "Other"),
			("organization_version", "Other"),
			("fiscal_year", "Other"),
			("currency", "USD"),
			("currency_precision", 2),
		):
			with self.subTest(field=field), self.assertRaises(ValueError):
				combine(approved(), (ref, replace(leaf, context=replace(leaf.context, **{field: value}))))

	def test_duplicate_sources_original_plans_and_facts_are_rejected(self):
		a = approved()
		with self.assertRaises(ValueError):
			combine(a, a)
		data = combine().as_dict()
		data["lines"].append(data["lines"][0])
		with self.assertRaises(ValueError):
			consolidated_snapshot_from_payload(data)

	def test_nested_consolidation_cannot_be_a_department_leaf(self):
		with self.assertRaises(ValueError):
			combine((approved()[0], combine()))

	def test_same_financial_period_from_two_original_versions_is_not_counted_twice(self):
		with self.assertRaises(ValueError):
			combine(approved(), approved("B", unit="UnitA", center="CenterA"))
		item = combine(approved(), approved("B", unit="UnitA", center="CenterA", period=2))
		self.assertEqual(len(item.lines), 2)

	def test_approved_source_identity_digest_and_applicant_must_match_leaf(self):
		ref, leaf = approved()
		for key, value in (
			("snapshot_digest", "d" * 64),
			("origin_plan", "Other"),
			("applicant", "Other"),
			("submission", "Other"),
		):
			with self.subTest(key=key), self.assertRaises(ValueError):
				combine((replace(ref, **{key: value}), leaf))

	def test_total_source_and_snapshot_proof_tampering_is_rejected(self):
		item = combine()
		for key, value in (
			("total_amount", "0.000000000"),
			("identity_key", "d" * 64),
			("source_set_digest", "d" * 64),
			("snapshot_digest", "d" * 64),
			("cost_center", "Fake"),
		):
			with self.subTest(key=key), self.assertRaises(ValueError):
				consolidated_snapshot_from_payload({**item.as_dict(), key: value})
		data = deepcopy(item.as_dict())
		data["lines"][0]["amount"] = "0.000000001"
		with self.assertRaises(ValueError):
			consolidated_snapshot_from_payload(data)

	def test_source_case_round_and_terminal_revision_are_bound(self):
		data = approved()[0].as_dict()
		for key, value in (("approval_round", 2), ("approval_revision", 1), ("approval_case", "Other")):
			with self.subTest(key=key), self.assertRaises(ValueError):
				ApprovedDepartment.from_payload({**data, key: value})

	def test_command_only_accepts_optimistic_references_and_compact_fingerprint(self):
		a, b = approved()[0], approved("B")[0]
		rows = [
			dict(
				submission=r.submission,
				expected_revision=1,
				approval_case=r.approval_case,
				expected_approval_revision=r.approval_revision,
				snapshot_digest=r.snapshot_digest,
			)
			for r in (a, b)
		]
		payload = dict(organization_unit="Root", subject="汇总", sources=rows)
		command = build_consolidation(payload)
		self.assertEqual(command, build_consolidation({**payload, "sources": rows[::-1]}))
		self.assertNotIn("sources", command.request_payload())
		with self.assertRaises(ValueError):
			build_consolidation({**payload, "total_amount": "1"})
		with self.assertRaises(ValueError):
			build_consolidation({**payload, "sources": rows + rows})

	def test_missing_or_extra_source_leaf_cannot_be_hidden(self):
		data = combine().as_dict()
		data["sources"] = data["sources"][:1]
		with self.assertRaises(ValueError):
			consolidated_snapshot_from_payload(data)


if __name__ == "__main__":
	unittest.main()
