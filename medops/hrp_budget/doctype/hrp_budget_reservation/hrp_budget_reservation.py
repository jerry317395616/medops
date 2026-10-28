"""Reservation aggregate admits only the private initial-write protocol."""

from __future__ import annotations

from medops.hrp_budget.services.initial_reservation_control import ROOT, require_initial_write
from medops.services.budget_documents import ControlledBudgetDocument
from medops.services.errors import raise_ione_error


class HRPBudgetReservation(ControlledBudgetDocument):
	"""Deny normal writes; the disabled internal protocol permits one initial post."""

	def before_insert(self):
		require_initial_write(self, doctype=ROOT, stage="insert", named=False)

	def validate(self):
		require_initial_write(self, doctype=ROOT, stage="insert" if self.is_new() else "submit")

	def before_submit(self):
		require_initial_write(self, doctype=ROOT, stage="submit")

	def db_insert(self, *args, **kwargs):
		state = require_initial_write(self, doctype=ROOT, stage="insert")
		result = super().db_insert(*args, **kwargs)
		state.root_inserted = True
		return result

	def db_update(self, *args, **kwargs):
		state = require_initial_write(self, doctype=ROOT, stage="submit")
		previous = self.get_doc_before_save()
		if not previous or int(previous.docstatus) != 0:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		result = super().db_update(*args, **kwargs)
		state.root_submitted = True
		return result

	def db_set(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")
