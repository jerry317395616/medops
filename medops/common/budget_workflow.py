from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from medops.common.budget_planning import object_payload
from medops.common.domain_service import canonical_json, fingerprint_json, normalize_sha256
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.organization import (
	normalize_positive_integer,
	normalize_reference,
	normalize_required_text,
)

CASE = "HRP Budget Approval"
REVIEW = "HRP Budget Review"
APPROVER_ROLE = "HRP Budget Approver"
WORKFLOW = "MedOps Budget Approval v1"
STATES = {name: "MedOps Budget " + name for name in ("Pending", "Approved", "Rejected", "Withdrawn")}
WORKFLOW_ACTIONS = {
	"Pending": "MedOps Record Vote",
	"Approved": "MedOps Approve",
	"Rejected": "MedOps Reject",
	"Withdrawn": "MedOps Withdraw",
}
CONFIRMATION_SECONDS = 300
MAX_CASE_ACTORS = 100
MAX_CASE_REVIEWS = 10000
_mutation: ContextVar[CaseMutation | None] = ContextVar("medops_budget_approval_mutation", default=None)


@dataclass(frozen=True, slots=True)
class CaseMutation:
	operation: str
	case: str
	expected_revision: int
	status: str = "Pending"
	current_step: int = 1
	review: str | None = None


@contextmanager
def approval_write(mutation: CaseMutation):
	if mutation.operation not in {"open", "confirm", "intent", "todo"}:
		raise ValueError("unknown budget approval operation")
	token = _mutation.set(mutation)
	try:
		yield
	finally:
		_mutation.reset(token)


def approval_mutation() -> CaseMutation | None:
	return _mutation.get()


@dataclass(frozen=True, slots=True)
class CaseRequest:
	submission: str
	expected_revision: int


def build_case_request(value: object) -> CaseRequest:
	data = object_payload(value, {"submission", "expected_revision"})
	return CaseRequest(
		submission=normalize_reference(data.get("submission"), label="submission"),
		expected_revision=normalize_positive_integer(
			data.get("expected_revision"), label="expected_revision"
		),
	)


@dataclass(frozen=True, slots=True)
class ReviewIntent:
	case: str
	expected_revision: int
	action: str
	reason: str


def build_review_intent(value: object) -> ReviewIntent:
	data = object_payload(value, {"case", "expected_revision", "action", "reason"})
	if data.get("action") not in {"Approve", "Reject", "Withdraw"}:
		raise ValueError("invalid human decision")
	return ReviewIntent(
		case=normalize_reference(data.get("case"), label="case"),
		expected_revision=normalize_positive_integer(
			data.get("expected_revision"), label="expected_revision"
		),
		action=data["action"],
		reason=normalize_required_text(data.get("reason"), label="reason", maximum=1000),
	)


@dataclass(frozen=True, slots=True)
class HumanConfirmation:
	confirmation: str
	proof: str
	acknowledge: bool


def build_human_confirmation(value: object) -> HumanConfirmation:
	data = object_payload(value, {"confirmation", "proof", "acknowledge"})
	if data.get("acknowledge") is not True:
		raise ValueError("explicit human acknowledgement required")
	proof = data.get("proof")
	if (
		not isinstance(proof, str)
		or not 32 <= len(proof) <= 128
		or any(
			not (character.isascii() and (character.isalnum() or character in "_-")) for character in proof
		)
	):
		raise ValueError("invalid human confirmation proof")
	return HumanConfirmation(
		confirmation=normalize_reference(data.get("confirmation"), label="confirmation"),
		proof=proof,
		acknowledge=True,
	)


def case_identity(submission: str, round_number: int) -> str:
	return fingerprint_json({"submission": submission, "round": round_number})


def parse_decision(value: object) -> dict[str, Any]:
	import json

	if isinstance(value, str):
		if len(value.encode("utf-8")) > 262144:
			raise ValueError("approval decision exceeds bounds")
		value = json.loads(value)
	if (
		not isinstance(value, dict)
		or type(value.get("schema_version")) is not int
		or value["schema_version"] != 2
	):
		raise ValueError("exact approval decision required")
	decision = json.loads(canonical_json(value))
	digest = normalize_sha256(decision.get("decision_digest"), label="decision_digest")
	decision["decision_digest"] = ""
	if fingerprint_json(decision) != digest:
		raise ValueError("approval decision digest differs")
	decision["decision_digest"] = digest
	if set(decision) != {
		"schema_version",
		"doctype",
		"docname",
		"matrix",
		"matrix_code",
		"matrix_name",
		"policy_version",
		"policy_digest",
		"approvers",
		"delegations",
		"steps",
		"decision_digest",
		"amount",
		"amount_precision",
		"context_digest",
		"policy_schema_version",
	}:
		raise ValueError("unexpected approval decision fields")
	if decision.get("doctype") != CASE:
		raise ValueError("budget approval target required")
	for key in ("docname", "matrix", "matrix_code", "matrix_name"):
		normalize_reference(decision.get(key), label=key)
	normalize_positive_integer(decision.get("policy_version"), label="policy_version")
	for key in ("policy_digest", "context_digest"):
		normalize_sha256(decision.get(key), label=key)
	if type(decision.get("policy_schema_version")) is not int or decision["policy_schema_version"] not in {
		1,
		2,
	}:
		raise ValueError("invalid policy amount version")
	precision = decision.get("amount_precision")
	if type(precision) is not int or not 0 <= precision <= 9:
		raise ValueError("invalid approval precision")
	exact_decimal_amount(decision.get("amount"), precision, maximum=MAX_APPROVAL_AMOUNT)
	steps = decision.get("steps")
	if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
		raise ValueError("invalid approval steps")
	all_users: set[str] = set()
	all_delegations: set[str] = set()
	for number, step in enumerate(steps, 1):
		if (
			not isinstance(step, dict)
			or type(step.get("sequence_no")) is not int
			or step["sequence_no"] != number
		):
			raise ValueError("non-contiguous approval steps")
		if step.get("approval_mode") not in {"All", "Any"}:
			raise ValueError("invalid approval mode")
		normalize_required_text(step.get("step_name"), label="step_name", maximum=140)
		if not isinstance(step.get("threshold_amount"), str):
			raise ValueError("exact step threshold required")
		exact_decimal_amount(step["threshold_amount"], maximum=MAX_APPROVAL_AMOUNT)
		users = step.get("approvers")
		sources = step.get("source_assignments")
		if (
			not isinstance(users, list)
			or not 1 <= len(users) <= MAX_CASE_ACTORS
			or not isinstance(sources, list)
		):
			raise ValueError("invalid approval assignments")
		user_ids = {
			normalize_reference(row.get("user"), label="approver") for row in users if isinstance(row, dict)
		}
		if len(user_ids) != len(users) or not 1 <= len(sources) <= MAX_CASE_ACTORS * 2:
			raise ValueError("duplicate or missing approval assignments")
		seen_sources = set()
		for row in sources:
			if not isinstance(row, dict) or row.get("source_type") not in {"Role", "User"}:
				raise ValueError("invalid approval provenance")
			if set(row) - {"user", "source_type", "source_value", "delegation", "delegated_from"}:
				raise ValueError("unexpected approval provenance")
			user = normalize_reference(row.get("user"), label="approver")
			if user not in user_ids:
				raise ValueError("unassigned approval provenance")
			normalize_reference(row.get("source_value"), label="source_value")
			if bool(row.get("delegation")) != bool(row.get("delegated_from")):
				raise ValueError("incomplete delegation provenance")
			for key in ("delegation", "delegated_from"):
				if row.get(key):
					normalize_reference(row[key], label=key)
			if row.get("delegation"):
				all_delegations.add(row["delegation"])
			identity = canonical_json(row)
			if identity in seen_sources:
				raise ValueError("duplicate approval provenance")
			seen_sources.add(identity)
		if {row["user"] for row in sources} != user_ids:
			raise ValueError("missing approval provenance")
		if any(row not in sources for row in users):
			raise ValueError("primary approver lacks its provenance")
		all_users.update(user_ids)
	if len(all_users) > MAX_CASE_ACTORS or sorted(all_users) != decision.get("approvers"):
		raise ValueError("invalid combined approval assignments")
	if sorted(all_delegations) != decision.get("delegations"):
		raise ValueError("invalid combined delegation evidence")
	return decision


@dataclass(frozen=True, slots=True)
class Vote:
	sequence_no: int
	actor: str
	action: str


@dataclass(frozen=True, slots=True)
class CaseProgress:
	status: str
	current_step: int
	current_voters: tuple[str, ...]


def replay_votes(decision: dict[str, Any], votes: tuple[Vote, ...], *, forbidden: set[str]) -> CaseProgress:
	decision = parse_decision(decision)
	if len(votes) > MAX_CASE_REVIEWS:
		raise ValueError("review history exceeds bounds")
	status = "Pending"
	current = 1
	accepted: set[str] = set()
	for vote in votes:
		if status != "Pending" or vote.sequence_no != current:
			raise ValueError("review outside the current approval step")
		if vote.action == "Withdraw":
			status, current = "Withdrawn", 0
			accepted = set()
			continue
		step = decision["steps"][current - 1]
		if vote.action not in {"Approve", "Reject"} or vote.actor in accepted:
			raise ValueError("duplicate or invalid approval vote")
		sources = [row for row in step["source_assignments"] if row["user"] == vote.actor]
		if (
			not sources
			or vote.actor in forbidden
			or any(row.get("delegated_from") in forbidden for row in sources)
		):
			raise ValueError("unassigned or conflicting approval actor")
		if vote.action == "Reject":
			status, current = "Rejected", 0
			accepted = set()
			continue
		accepted.add(vote.actor)
		required = {row["user"] for row in step["approvers"]}
		if step["approval_mode"] == "Any" or accepted == required:
			accepted = set()
			current += 1
			if current > len(decision["steps"]):
				status, current = "Approved", 0
	return CaseProgress(status, current, tuple(sorted(accepted)))


def vote_identity(case: str, vote: Vote) -> str:
	return fingerprint_json(
		{
			"case": case,
			"step": vote.sequence_no,
			"actor": vote.actor,
			"kind": "Withdraw" if vote.action == "Withdraw" else "Vote",
		}
	)
