"""Reservation confirmation storage remains closed until its service exists."""

from __future__ import annotations

from medops.services.budget_documents import ControlledBudgetDocument
from medops.services.errors import raise_ione_error


class HRPBudgetReservationAuthorization(ControlledBudgetDocument):
	"""Deny standard document mutation while authorization is unconnected."""

	def before_insert(self):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def validate(self):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_insert(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_update(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def db_set(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")
