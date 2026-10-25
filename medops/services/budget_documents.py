from __future__ import annotations

from typing import Any

from frappe.model.document import Document

from medops.common.budget_planning import (
	DIMENSION,
	FUND,
	LINE,
	MASTER_TYPES,
	MAX_LINES,
	VALUE,
	BudgetPlanningError,
	PlanLine,
	amount_total,
	build_master,
	decimal_amount,
	line_identity,
	master_identity,
	master_write_active,
	planning_operation,
)
from medops.common.domain_service import fingerprint_json, normalize_sha256
from medops.services.errors import raise_ione_error


class ControlledBudgetDocument(Document):
	def on_trash(self):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_rename(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_cancel(self):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_update_after_submit(self):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def _next_revision(self):
		if self.is_new():
			self.revision = 1
			return
		before = self.get_doc_before_save()
		locked = self.flags.locked_revision
		if not before or locked is None or int(before.revision) != int(locked):
			raise_ione_error("CONFLICT")
		self.revision = int(locked) + 1

	def _immutable(self, fields: tuple[str, ...]):
		before = self.get_doc_before_save()
		if before and any(str(before.get(key) or "") != str(self.get(key) or "") for key in fields):
			raise_ione_error("OPERATION_NOT_ALLOWED")


class BudgetMasterDocument(ControlledBudgetDocument):
	def validate(self):
		if not master_write_active() or self.docstatus:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		kind = next((key for key, name in MASTER_TYPES.items() if name == self.doctype), None)
		fields = (
			"company",
			"hospital",
			"code",
			"display_name",
			"enabled",
			"valid_from",
			"valid_to",
			"remarks",
		)
		payload = {key: self.get(key) for key in fields}
		for key in ("valid_from", "valid_to"):
			payload[key] = str(self.get(key)) if self.get(key) else None
		extra = {DIMENSION: "purpose", VALUE: "dimension", FUND: "source_type"}[self.doctype]
		payload[extra] = self.get(extra)
		try:
			command = build_master(kind, payload)
			if self.identity_key != master_identity(command):
				raise_ione_error("CONFIGURATION_INVALID")
			for key in (*fields, extra):
				self.set(key, getattr(command, key))
		except (ValueError, TypeError) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		self._immutable(("company", "hospital", "identity_key", "code", "dimension", "purpose"))

	def before_save(self):
		if not master_write_active():
			raise_ione_error("OPERATION_NOT_ALLOWED")
		self._next_revision()

	def as_public_dict(self) -> dict[str, Any]:
		fields = [
			"name",
			"company",
			"hospital",
			"code",
			"display_name",
			"enabled",
			"valid_from",
			"valid_to",
			"revision",
			"remarks",
		]
		fields.append({DIMENSION: "purpose", VALUE: "dimension", FUND: "source_type"}[self.doctype])
		return {
			key: str(self.get(key)) if key in {"valid_from", "valid_to"} and self.get(key) else self.get(key)
			for key in fields
		}


class BudgetVersionDocument(ControlledBudgetDocument):
	def validate(self):
		op = planning_operation()
		before = self.get_doc_before_save()
		if op not in {"save_version", "clone", "freeze"} or (before and before.docstatus != 0):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if op == "freeze":
			if self.docstatus != 1 or self.status != "Frozen" or not before:
				raise_ione_error("INVALID_STATE_TRANSITION")
			try:
				normalize_sha256(self.snapshot_digest, label="snapshot_digest")
			except ValueError as exc:
				raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		else:
			if (
				self.docstatus
				or self.status != "Draft"
				or self.snapshot_digest
				or self.frozen_at
				or self.frozen_by
			):
				raise_ione_error("INVALID_STATE_TRANSITION")
		self._immutable(
			(
				"company",
				"hospital",
				"organization_version",
				"fiscal_year",
				"currency",
				"currency_precision",
				"version_number",
				"period_start",
				"period_end",
				"distribution_frequency",
				"cloned_from",
			)
		)

	def before_save(self):
		if planning_operation() not in {"save_version", "clone"}:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		self._next_revision()

	def before_submit(self):
		if planning_operation() != "freeze":
			raise_ione_error("OPERATION_NOT_ALLOWED")
		self._next_revision()

	def as_public_dict(self) -> dict[str, Any]:
		fields = (
			"name",
			"company",
			"hospital",
			"organization_version",
			"fiscal_year",
			"version_number",
			"subject",
			"currency",
			"currency_precision",
			"period_start",
			"period_end",
			"distribution_frequency",
			"status",
			"docstatus",
			"revision",
			"cloned_from",
			"frozen_at",
			"frozen_by",
			"remarks",
		)
		return {
			key: str(self.get(key))
			if key in {"period_start", "period_end", "frozen_at"} and self.get(key)
			else self.get(key)
			for key in fields
		}


class BudgetPlanDocument(ControlledBudgetDocument):
	def validate(self):
		if planning_operation() not in {"save_plan", "clone", "withdraw"} or self.docstatus:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if self.status not in {"Draft", "Withdrawn"}:
			raise_ione_error("INVALID_STATE_TRANSITION")
		if not self.lines or len(self.lines) > MAX_LINES:
			raise_ione_error("INVALID_REQUEST")
		try:
			seen = set()
			for row in self.lines:
				row.amount = decimal_amount(row.amount, int(self.currency_precision))
				line = PlanLine(
					budget_subject=row.budget_subject,
					funding_source=row.funding_source,
					account=row.account,
					project=row.project or None,
					period_index=int(row.period_index),
					amount=row.amount,
					description=row.description or None,
				)
				if row.line_key != line_identity(line) or row.line_key in seen:
					raise BudgetPlanningError("invalid or duplicate line identity")
				seen.add(row.line_key)
			self.total_amount = amount_total([row.amount for row in self.lines], int(self.currency_precision))
			if self.identity_key != fingerprint_json(
				{"budget_version": self.budget_version, "organization_unit": self.organization_unit}
			):
				raise BudgetPlanningError("invalid plan identity")
		except (ValueError, TypeError) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		self._immutable(
			(
				"identity_key",
				"company",
				"hospital",
				"organization_version",
				"organization_unit",
				"cost_center",
				"budget_version",
				"fiscal_year",
				"currency",
				"currency_precision",
				"applicant",
				"posting_date",
				"source_plan",
			)
		)

	def before_save(self):
		if planning_operation() not in {"save_plan", "clone", "withdraw"}:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		self._next_revision()

	def before_submit(self):
		# Financial approval is implemented through the following governed workflow slice.
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def as_public_dict(self) -> dict[str, Any]:
		fields = (
			"name",
			"company",
			"hospital",
			"budget_version",
			"organization_version",
			"organization_unit",
			"cost_center",
			"fiscal_year",
			"currency",
			"currency_precision",
			"subject",
			"status",
			"applicant",
			"posting_date",
			"revision",
			"remarks",
			"docstatus",
		)
		result = {key: str(self.get(key)) if key == "posting_date" else self.get(key) for key in fields}
		result["total_amount"] = decimal_amount(self.total_amount, int(self.currency_precision))
		result["lines"] = [
			{
				key: str(row.get(key)) if key in {"period_start", "period_end"} else row.get(key)
				for key in (
					"budget_subject",
					"funding_source",
					"account",
					"project",
					"period_index",
					"period_start",
					"period_end",
					"amount",
					"description",
				)
			}
			for row in self.lines
		]
		return result


class BudgetPlanLineDocument(ControlledBudgetDocument):
	def validate(self):
		if self.doctype != LINE or planning_operation() not in {"save_plan", "clone"}:
			raise_ione_error("OPERATION_NOT_ALLOWED")
