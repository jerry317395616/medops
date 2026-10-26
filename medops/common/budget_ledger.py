"""Exact financial intent and original-grain derivation; authorization lives in services."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from datetime import time
from decimal import localcontext
from typing import Any

from medops.common.budget_consolidation import (
	ConsolidatedSnapshot,
	consolidated_snapshot_from_payload,
)
from medops.common.budget_planning import object_payload
from medops.common.budget_workflow import case_identity
from medops.common.domain_service import canonical_json, fingerprint_json, normalize_sha256
from medops.common.exact_money import signed_decimal_amount
from medops.common.immutable_ledger import ImmutableLedgerDefinition
from medops.common.organization import (
	normalize_positive_integer,
	normalize_reference,
	normalize_required_date,
	normalize_required_text,
)

LEDGER = "HRP Budget Ledger"
RELEASE = "HRP Budget Release"
RELEASE_ENTRY = "HRP Budget Release Entry"
LEDGER_WRITER_ROLES = frozenset({"System Manager", "HRP System Manager", "HRP Budget Manager"})
BUDGET_LEDGER_DEFINITION = ImmutableLedgerDefinition(
	doctype=LEDGER,
	required_roles=LEDGER_WRITER_ROLES,
	negated_fields=("amount",),
	swapped_field_pairs=(("debit", "credit"),),
	reversal_override_fields=frozenset(
		{
			"posting_date",
			"posting_time",
			"voucher_type",
			"voucher_no",
			"source_hash",
			"entry_key",
			"release_digest",
		}
	),
	required_reversal_override_fields=frozenset(
		{
			"posting_date",
			"posting_time",
			"voucher_type",
			"voucher_no",
			"source_hash",
			"entry_key",
			"release_digest",
		}
	),
	amount_schema_version=2,
)


@dataclass(frozen=True, slots=True)
class ReleaseRequest:
	approval_case: str
	expected_approval_revision: int
	posting_date: str
	reason: str


def build_release_request(value: object) -> ReleaseRequest:
	data = object_payload(value, {"approval_case", "expected_approval_revision", "posting_date", "reason"})
	revision = normalize_positive_integer(
		data.get("expected_approval_revision"), label="expected_approval_revision"
	)
	if revision < 2:
		raise ValueError("completed approval revision required")
	return ReleaseRequest(
		approval_case=normalize_reference(data.get("approval_case"), label="approval_case"),
		expected_approval_revision=revision,
		posting_date=normalize_required_date(data.get("posting_date"), label="posting_date"),
		reason=normalize_required_text(data.get("reason"), label="reason", maximum=1000),
	)


@dataclass(frozen=True, slots=True)
class ReverseRequest:
	release: str
	expected_revision: int
	posting_date: str
	reason: str


def build_reverse_request(value: object) -> ReverseRequest:
	data = object_payload(value, {"release", "expected_revision", "posting_date", "reason"})
	return ReverseRequest(
		release=normalize_reference(data.get("release"), label="release"),
		expected_revision=normalize_positive_integer(
			data.get("expected_revision"), label="expected_revision"
		),
		posting_date=normalize_required_date(data.get("posting_date"), label="posting_date"),
		reason=normalize_required_text(data.get("reason"), label="reason", maximum=1000),
	)


@dataclass(frozen=True, slots=True)
class FinalBudgetApproval:
	submission: str
	snapshot_digest: str
	approval_case: str
	approval_round: int
	approval_revision: int
	decision_digest: str
	final_review: str
	event_digest: str

	@classmethod
	def from_payload(cls, value: object) -> FinalBudgetApproval:
		data = object_payload(value, set(cls.__dataclass_fields__))
		result = cls(
			submission=normalize_reference(data.get("submission"), label="submission"),
			approval_case=normalize_reference(data.get("approval_case"), label="approval_case"),
			final_review=normalize_reference(data.get("final_review"), label="final_review"),
			snapshot_digest=normalize_sha256(data.get("snapshot_digest"), label="snapshot_digest"),
			decision_digest=normalize_sha256(data.get("decision_digest"), label="decision_digest"),
			event_digest=normalize_sha256(data.get("event_digest"), label="event_digest"),
			approval_round=normalize_positive_integer(data.get("approval_round"), label="approval_round"),
			approval_revision=normalize_positive_integer(
				data.get("approval_revision"), label="approval_revision"
			),
		)
		if (
			result.approval_revision < 2
			or case_identity(result.submission, result.approval_round) != result.approval_case
		):
			raise ValueError("invalid final budget approval identity")
		return result


@dataclass(frozen=True, slots=True)
class BudgetReleaseFacts:
	operation: str
	snapshot: ConsolidatedSnapshot
	approval: FinalBudgetApproval
	posting_date: str
	reason: str
	prepared_by: str
	total_amount: str
	reversal_of: str | None = None
	original_intent_digest: str | None = None

	@property
	def identity_key(self) -> str:
		return fingerprint_json(
			{"operation": self.operation, "approval_case": self.approval.approval_case}
			if self.operation == "Release"
			else {"operation": self.operation, "reversal_of": self.reversal_of}
		)

	def facts(self) -> dict[str, Any]:
		return dict(
			schema_version=1,
			operation=self.operation,
			**asdict(self.snapshot.context),
			amount_schema_version=2,
			organization_unit=self.snapshot.organization_unit,
			source_submission=self.approval.submission,
			source_snapshot_digest=self.approval.snapshot_digest,
			source_approval=self.approval.approval_case,
			source_approval_round=self.approval.approval_round,
			source_approval_revision=self.approval.approval_revision,
			source_decision_digest=self.approval.decision_digest,
			source_final_review=self.approval.final_review,
			source_event_digest=self.approval.event_digest,
			posting_date=self.posting_date,
			reason=self.reason,
			prepared_by=self.prepared_by,
			total_amount=self.total_amount,
			line_count=len(self.snapshot.lines),
			reversal_of=self.reversal_of,
			original_intent_digest=self.original_intent_digest,
		)

	@property
	def intent_digest(self) -> str:
		return fingerprint_json(self.facts())


def release_facts(
	snapshot: ConsolidatedSnapshot,
	approval: FinalBudgetApproval,
	*,
	posting_date: object,
	reason: object,
	prepared_by: object,
) -> BudgetReleaseFacts:
	"""Validate sealed facts; this is not a grant to post or a human confirmation."""
	if not isinstance(snapshot, ConsolidatedSnapshot) or not isinstance(approval, FinalBudgetApproval):
		raise ValueError("consolidated exact source and final approval required")
	with localcontext() as context:
		context.prec = 48
		snapshot = consolidated_snapshot_from_payload(snapshot.as_dict())
		approval = FinalBudgetApproval.from_payload(asdict(approval))
		if (
			approval.submission != snapshot.identity_key
			or approval.snapshot_digest != snapshot.snapshot_digest
		):
			raise ValueError("final approval differs from sealed source")
		total = snapshot.total_amount
	return BudgetReleaseFacts(
		operation="Release",
		snapshot=snapshot,
		approval=approval,
		posting_date=normalize_required_date(posting_date, label="posting_date"),
		reason=normalize_required_text(reason, label="reason", maximum=1000),
		prepared_by=normalize_reference(prepared_by, label="prepared_by"),
		total_amount=total,
	)


def reversal_facts(
	original: BudgetReleaseFacts, *, posting_date: object, reason: object, prepared_by: object
) -> BudgetReleaseFacts:
	if (
		not isinstance(original, BudgetReleaseFacts)
		or original.operation != "Release"
		or original.reversal_of
	):
		raise ValueError("only a complete original allocation can be reversed")
	canonical = release_facts(
		original.snapshot,
		original.approval,
		posting_date=original.posting_date,
		reason=original.reason,
		prepared_by=original.prepared_by,
	)
	if original != canonical:
		raise ValueError("original release intent differs from sealed source")
	return replace(
		canonical,
		operation="Reverse",
		posting_date=normalize_required_date(posting_date, label="posting_date"),
		reason=normalize_required_text(reason, label="reason", maximum=1000),
		prepared_by=normalize_reference(prepared_by, label="prepared_by"),
		total_amount=signed_decimal_amount(
			"-" + canonical.total_amount, canonical.snapshot.context.currency_precision
		),
		reversal_of=original.identity_key,
		original_intent_digest=original.intent_digest,
	)


def ledger_entry_key(release: str, fact_key: str) -> str:
	return fingerprint_json(
		dict(
			release=normalize_sha256(release, label="release"),
			fact_key=normalize_sha256(fact_key, label="fact_key"),
		)
	)


def allocation_values(facts: BudgetReleaseFacts, *, posting_time: str) -> Iterator[dict[str, Any]]:
	"""Derive each original allocation once; callers own fresh authority and atomicity."""
	if not isinstance(facts, BudgetReleaseFacts) or facts.operation != "Release":
		raise ValueError("original budget release facts required")
	canonical = release_facts(
		facts.snapshot,
		facts.approval,
		posting_date=facts.posting_date,
		reason=facts.reason,
		prepared_by=facts.prepared_by,
	)
	if canonical != facts:
		raise ValueError("release intent differs from sealed source")
	if (
		not isinstance(posting_time, str)
		or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?", posting_time) is None
	):
		raise ValueError("posting time required")
	try:
		clock = time.fromisoformat(posting_time)
	except ValueError as error:
		raise ValueError("invalid posting time") from error
	if clock.tzinfo is not None:
		raise ValueError("local posting time required")
	posting_time = clock.isoformat()
	context = facts.snapshot.context
	zero = signed_decimal_amount("0", context.currency_precision)
	references = {source.origin_plan: source for source in facts.snapshot.sources}
	for line in facts.snapshot.lines:
		source = references[line.origin_plan]
		dimensions = {
			key: getattr(line, key)
			for key in (
				"organization_unit",
				"cost_center",
				"budget_subject",
				"funding_source",
				"account",
				"project",
				"period_index",
				"period_start",
				"period_end",
			)
		}
		values = dict(
			**asdict(context),
			**asdict(line),
			amount_schema_version=2,
			fact_key=line.fact_key,
			entry_type="Allocation",
			entry_key=ledger_entry_key(facts.identity_key, line.fact_key),
			posting_date=facts.posting_date,
			posting_time=posting_time,
			voucher_type=RELEASE,
			voucher_no=facts.identity_key,
			reference_type="HRP Budget Submission",
			reference_name=source.submission,
			quantity=0.0,
			debit=line.amount,
			credit=zero,
			dimensions_json=canonical_json(dimensions),
			release_digest=facts.intent_digest,
			source_submission=source.submission,
			source_approval=source.approval_case,
			source_snapshot_digest=source.snapshot_digest,
			source_decision_digest=source.approval_decision_digest,
			source_final_review=source.approval_final_review,
			source_event_digest=source.approval_event_digest,
		)
		values["source_hash"] = fingerprint_json(values)
		yield values
