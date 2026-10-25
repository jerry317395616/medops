from __future__ import annotations

import frappe

from medops.services.errors import raise_ione_error

REFERENCES = {
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
	for doctype, field in REFERENCES.get(doc.doctype, ()):
		if frappe.db.table_exists(doctype) and frappe.db.exists(doctype, {field: doc.name}):
			raise_ione_error("OPERATION_NOT_ALLOWED")
