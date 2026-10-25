from __future__ import annotations

from dataclasses import fields
from typing import Any

from medops.common.budget_approval import (
	DepartmentSnapshot,
	SubmissionLine,
	department_snapshot_from_payload,
	submission_sealing_active,
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
	keys |= {item.name for item in fields(DepartmentSnapshot)} - {"context", "lines"}
	result = {key: doc.get(key) for key in keys}
	result["posting_date"] = str(doc.posting_date)
	result["lines"] = [
		{
			key: str(row.get(key)) if key in {"period_start", "period_end"} else row.get(key)
			for key in ("fact_key", *(item.name for item in fields(SubmissionLine)))
		}
		for row in doc.lines or []
	]
	return result


class BudgetSubmissionDocument(ControlledBudgetDocument):
	def validate(self):
		if not submission_sealing_active() or self.docstatus not in {0, 1} or self.status != "Sealed":
			raise_ione_error("OPERATION_NOT_ALLOWED")
		before = self.get_doc_before_save()
		if before and (before.docstatus != 0 or self.docstatus != 1):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		try:
			snapshot = department_snapshot_from_payload(stored_facts(self))
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
		facts = department_snapshot_from_payload(stored_facts(self)).as_dict()
		# The whole-version proof covers other departments too. It remains an
		# internal integrity value, matching the original version's field level.
		facts.pop("source_version_digest")
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
