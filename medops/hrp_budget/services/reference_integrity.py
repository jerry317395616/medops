from __future__ import annotations

import frappe

from medops.services.errors import raise_ione_error

REFERENCES = {
	"User": (
		("HRP Budget Version", "frozen_by"),
		("HRP Budget Plan", "applicant"),
		("HRP Budget Submission", "applicant"),
		("HRP Budget Submission", "sealed_by"),
		("HRP Budget Approval", "applicant"),
		("HRP Budget Approval", "source_applicant"),
		("HRP Budget Approval", "requested_by"),
		("HRP Budget Review", "actor"),
		("HRP Approval Matrix Row", "approver_user"),
		("HRP Delegation", "from_user"),
		("HRP Delegation", "to_user"),
	),
	"Company": (
		("HRP Budget Version", "company"),
		("HRP Budget Plan", "company"),
		("HRP Business Dimension", "company"),
		("HRP Dimension Value", "company"),
		("HRP Funding Source", "company"),
	),
	"Currency": (("HRP Budget Version", "currency"),),
	"Fiscal Year": (("HRP Budget Version", "fiscal_year"),),
	"Cost Center": (("HRP Budget Plan", "cost_center"),),
	"Account": (("HRP Budget Plan Line", "account"),),
	"Project": (("HRP Budget Plan Line", "project"),),
}


def protect_reference_rename(doc, method=None, *args, **kwargs):
	"""Native rename/merge rewrites linked rows, including sealed planning facts.

	Referenced identifiers require an explicitly reviewed migration instead of the
	generic native rename endpoint. Labels and other native operations keep their
	standard controllers. This hook never writes to standard ERP documents.
	"""
	del method, args, kwargs
	references = REFERENCES.get(doc.doctype)
	if not references:
		return
	# The legacy native RPC invokes before_rename before validate_rename checks
	# permissions. Do not inspect HRP references or acquire their locks first.
	doc.check_permission("write")
	# Share the source fence with planning/master-data writers. Native rename
	# loaded doc through a consistent read before this hook; both the source
	# and reference lookups must be current reads after a possible lock wait.
	try:
		if not frappe.db.get_value(doc.doctype, doc.name, "name", for_update=True):
			raise_ione_error("CONFLICT")
		for doctype, field in references:
			if frappe.db.table_exists(doctype) and frappe.db.get_value(
				doctype, {field: doc.name}, "name", for_update=True
			):
				raise_ione_error("OPERATION_NOT_ALLOWED")
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError) as exc:
		# The outer native request owns rollback/retry. A rejected current read
		# is a controlled conflict, never permission to use the earlier snapshot.
		raise_ione_error("CONFLICT", cause=exc)
