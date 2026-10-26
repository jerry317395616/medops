from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from medops.common.budget_workflow import (
	CASE,
	REVIEW,
	STATES,
	Vote,
	approval_mutation,
	case_identity,
	parse_decision,
	vote_identity,
)
from medops.common.domain_service import fingerprint_json
from medops.common.exact_money import exact_decimal_amount
from medops.hrp_budget.services.approval_evidence import decision_history, progress_for, validate_evidence
from medops.services.budget_documents import ControlledBudgetDocument
from medops.services.errors import raise_ione_error


class BudgetApprovalDocument(ControlledBudgetDocument):
	def before_validate(self):
		mutation = approval_mutation()
		if not mutation or mutation.case != self.name or mutation.operation not in {"open", "confirm"}:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if mutation.operation == "confirm":
			before = self.get_doc_before_save()
			if not before or before.status != "Pending" or int(before.revision) != mutation.expected_revision:
				raise_ione_error("CONFLICT")
			if self.workflow_state != STATES[mutation.status]:
				raise_ione_error("INVALID_STATE_TRANSITION")
			self.status = mutation.status
			self.current_step = mutation.current_step
			self.revision = mutation.expected_revision + 1
			self.active_key = self.submission if self.status == "Pending" else None
			self.completed_at = now_datetime() if self.status != "Pending" else None
			self.final_review = mutation.review if self.status != "Pending" else None

	def validate(self):
		mutation = approval_mutation()
		if not mutation or mutation.case != self.name or mutation.operation not in {"open", "confirm"}:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		try:
			decision = parse_decision(self.decision_snapshot)
			if (
				self.identity_key != case_identity(self.submission, int(self.round_number))
				or self.name != self.identity_key
			):
				raise ValueError("invalid case identity")
			if (
				decision["docname"] != self.name
				or decision["matrix"] != self.policy_matrix
				or decision["policy_version"] != int(self.policy_revision)
				or decision["policy_digest"] != self.policy_digest
				or decision["decision_digest"] != self.decision_digest
			):
				raise ValueError("invalid policy snapshot")
			if (
				decision["amount_precision"] != int(self.currency_precision)
				or exact_decimal_amount(decision["amount"], int(self.currency_precision)) != self.total_amount
			):
				raise ValueError("invalid case money")
			if self.workflow_state != STATES[self.status] or int(self.docstatus) != (
				1 if self.status == "Approved" else 0
			):
				raise ValueError("invalid case lifecycle")
		except (ValueError, TypeError, KeyError) as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		if mutation.operation == "open":
			if (
				not self.is_new()
				or int(self.revision) != 1
				or self.status != "Pending"
				or int(self.current_step) != 1
				or self.active_key != self.submission
				or self.completed_at
				or self.final_review
			):
				raise_ione_error("INVALID_STATE_TRANSITION")
		else:
			before = self.get_doc_before_save()
			self._immutable(
				tuple(
					field.fieldname
					for field in self.meta.fields
					if field.fieldname
					not in {
						"status",
						"workflow_state",
						"current_step",
						"revision",
						"active_key",
						"completed_at",
						"final_review",
					}
				)
			)
			events = decision_history(self, lock=True)
			if (
				not before
				or len(events) != int(before.revision)
				or not events
				or events[-1].name != mutation.review
			):
				raise_ione_error("CONFIGURATION_INVALID")
			progress = progress_for(self, events)
			if progress.status != self.status or progress.current_step != int(self.current_step):
				raise_ione_error("CONFIGURATION_INVALID")

	def before_save(self):
		if not approval_mutation():
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_submit(self):
		mutation = approval_mutation()
		if not mutation or mutation.operation != "confirm" or self.status != "Approved":
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def as_public_dict(self):
		return {
			key: str(self.get(key))
			if key in {"posting_date", "requested_at", "completed_at"} and self.get(key)
			else self.get(key)
			for key in (
				"name",
				"submission",
				"round_number",
				"company",
				"hospital",
				"organization_version",
				"organization_unit",
				"cost_center",
				"fiscal_year",
				"currency",
				"currency_precision",
				"subject",
				"total_amount",
				"source_digest",
				"applicant",
				"source_applicant",
				"requested_by",
				"posting_date",
				"status",
				"current_step",
				"policy_matrix",
				"policy_revision",
				"policy_digest",
				"decision_digest",
				"revision",
				"requested_at",
				"completed_at",
				"final_review",
				"docstatus",
			)
		}


class BudgetReviewDocument(ControlledBudgetDocument):
	def validate(self):
		mutation = approval_mutation()
		if (
			not mutation
			or mutation.case != self.case
			or not self.is_new()
			or self.docstatus
			or self.actor != frappe.session.user
		):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if (
			(self.event_kind == "Confirmation" and mutation.operation != "intent")
			or (self.event_kind == "Decision" and mutation.operation != "confirm")
			or self.event_kind not in {"Confirmation", "Decision"}
		):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		case = frappe.get_doc(CASE, self.case)
		if (
			case.status != "Pending"
			or int(case.revision) != int(self.case_revision)
			or int(case.current_step) != int(self.sequence_no)
		):
			raise_ione_error("CONFLICT")
		if any(
			self.get(key) != case.get(key)
			for key in (
				"company",
				"hospital",
				"organization_unit",
				"source_digest",
				"policy_digest",
				"decision_digest",
			)
		):
			raise_ione_error("CONFIGURATION_INVALID")
		if self.event_kind == "Confirmation" and (self.confirmation or self.vote_key or self.revision_key):
			raise_ione_error("CONFIGURATION_INVALID")
		if self.event_kind == "Decision":
			if (
				not self.confirmation
				or self.vote_key
				!= vote_identity(self.case, Vote(int(self.sequence_no), self.actor, self.action))
				or self.revision_key != fingerprint_json([self.case, int(self.case_revision)])
			):
				raise_ione_error("CONFIGURATION_INVALID")
			original = frappe.get_doc(REVIEW, self.confirmation)
			validate_evidence(original)
			if original.event_kind != "Confirmation" or any(
				original.get(key) != self.get(key)
				for key in (
					"case",
					"action",
					"sequence_no",
					"actor",
					"case_revision",
					"source_digest",
					"policy_digest",
					"decision_digest",
					"sod_digest",
					"reason",
					"confirmation_hash",
					"session_digest",
				)
			):
				raise_ione_error("CONFIGURATION_INVALID")
			if str(original.expires_at) != str(self.expires_at):
				raise_ione_error("CONFIGURATION_INVALID")
		validate_evidence(self)
