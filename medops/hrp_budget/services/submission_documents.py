from __future__ import annotations

import json
from dataclasses import fields
from typing import Any

from medops.common.budget_approval import (
	DepartmentSnapshot,
	SubmissionLine,
	department_snapshot_from_payload,
	submission_sealing_active,
)
from medops.common.budget_consolidation import (
	MAX_SOURCE_PROOF_BYTES,
	ConsolidatedSnapshot,
	consolidated_snapshot_from_payload,
)
from medops.services.budget_documents import ControlledBudgetDocument
from medops.services.errors import raise_ione_error


def stored_facts(doc) -> dict[str, Any]:
	keys = {
		"schema_version",
		"submission_type",
		"identity_key",
		"snapshot_digest",
		"total_amount",
		"company",
		"hospital",
		"organization_version",
		"fiscal_year",
		"currency",
		"currency_precision",
	}
	aggregate = doc.submission_type == "Consolidated"
	keys |= {item.name for item in fields(ConsolidatedSnapshot if aggregate else DepartmentSnapshot)} - {
		"context",
		"lines",
		"sources",
	}
	if aggregate:
		keys |= {"cost_center", "source_set_digest"}
	result = {key: doc.get(key) for key in keys}
	if aggregate:
		proof = doc.get("sources_snapshot")
		if not isinstance(proof, str) or len(proof.encode("utf-8")) > MAX_SOURCE_PROOF_BYTES:
			raise ValueError("invalid source proof")
		result["sources"] = json.loads(proof)
	result["posting_date"] = str(doc.posting_date)
	result["lines"] = [
		{
			key: str(row.get(key)) if key in {"period_start", "period_end"} else row.get(key)
			for key in ("fact_key", *(item.name for item in fields(SubmissionLine)))
		}
		for row in doc.lines or []
	]
	return result


def snapshot_from_document(doc):
	return (
		consolidated_snapshot_from_payload(stored_facts(doc))
		if doc.submission_type == "Consolidated"
		else department_snapshot_from_payload(stored_facts(doc))
	)


class BudgetSubmissionDocument(ControlledBudgetDocument):
	def validate(self):
		if not submission_sealing_active() or self.docstatus not in {0, 1} or self.status != "Sealed":
			raise_ione_error("OPERATION_NOT_ALLOWED")
		before = self.get_doc_before_save()
		if before and (before.docstatus != 0 or self.docstatus != 1):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		try:
			snapshot = snapshot_from_document(self)
			if self.submission_type == "Consolidated":
				if any(
					self.get(key) not in (None, "", 0)
					for key in (
						"source_plan",
						"source_plan_revision",
						"source_plan_digest",
						"source_version",
						"source_version_digest",
					)
				):
					raise ValueError("consolidated header contains a synthetic source")
			elif self.get("sources_snapshot") or self.get("source_set_digest"):
				raise ValueError("department header contains consolidated sources")
			if self.revision != 1 or not self.sealed_by or not self.sealed_at:
				raise ValueError("missing sealing evidence")
			if before and stored_facts(before) != stored_facts(self):
				raise ValueError("sealed facts cannot change on submit")
			if self.sealed_by != self.applicant:
				raise ValueError("sealing actor differs from applicant")
			if snapshot.identity_key != self.identity_key:
				raise ValueError("invalid submission identity")
		except (ValueError, TypeError, KeyError) as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)

	def before_save(self):
		if not submission_sealing_active() or not self.is_new() or self.docstatus != 0:
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_submit(self):
		if not submission_sealing_active():
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def as_public_dict(self) -> dict[str, Any]:
		facts = snapshot_from_document(self).as_dict()
		# The whole-version proof covers other departments too. It remains an
		# internal integrity value, matching the original version's field level.
		facts.pop("source_version_digest", None)
		for row in facts["lines"]:
			row.pop("origin_version_digest")
		return {
			"name": self.name,
			"docstatus": int(self.docstatus),
			"status": self.status,
			"revision": int(self.revision),
			"sealed_by": self.sealed_by,
			"sealed_at": str(self.sealed_at),
			**facts,
		}


class BudgetSubmissionLineDocument(ControlledBudgetDocument):
	def validate(self):
		if not submission_sealing_active():
			raise_ione_error("OPERATION_NOT_ALLOWED")
