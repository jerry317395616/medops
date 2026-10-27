"""Append only an exact row supplied by the owned evidence service."""

from __future__ import annotations

from medops.common.reservation_evidence_control import expected_evidence_values
from medops.hrp_budget.services.release_documents import same_value
from medops.services.budget_documents import ControlledBudgetDocument
from medops.services.errors import raise_ione_error


class HRPBudgetReservationAuthorization(ControlledBudgetDocument):
	"""No standard permissions or updates; the private service may append."""

	def _require_expected(self, *, named=False):
		values = expected_evidence_values()
		if not values or not self.is_new() or self.docstatus:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if named and self.name != values["name"]:
			raise_ione_error("CONFIGURATION_INVALID")
		for field in self.meta.fields:
			if field.fieldtype in {"Section Break", "Column Break", "Tab Break"}:
				continue
			if field.fieldname not in values or not same_value(
				field, self.get(field.fieldname), values[field.fieldname]
			):
				raise_ione_error("CONFIGURATION_INVALID")

	def before_insert(self):
		self._require_expected()

	def validate(self):
		self._require_expected(named=True)

	def db_insert(self, *args, **kwargs):
		self._require_expected(named=True)
		return super().db_insert(*args, **kwargs)

	def db_update(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_set(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")
