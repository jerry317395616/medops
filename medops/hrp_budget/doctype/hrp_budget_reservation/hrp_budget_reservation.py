"""Reservation aggregate remains closed until the authorized service exists."""

from __future__ import annotations

from medops.services.budget_documents import ControlledBudgetDocument
from medops.services.errors import raise_ione_error


class HRPBudgetReservation(ControlledBudgetDocument):
	"""Do not permit document mutation before the complete transaction exists."""

	def before_insert(self):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def validate(self):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_submit(self):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_insert(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_update(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_set(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")
