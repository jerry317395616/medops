from __future__ import annotations

import frappe
from frappe.core.doctype.report.report import Report

from medops.services.core_reports import core_report_code, reject_added_sources


class CoreReportAccessMixin(Report):
	def is_permitted(self):
		if core_report_code(doc=self):
			# Native get_report_doc calls this before choosing a prepared result, including
			# saved custom descendants. Change only this in-memory document, never the DB.
			self.prepared_report = 0
			reject_added_sources(self.get("custom_columns"))
			reject_added_sources(
				frappe.form_dict.get("custom_columns") if getattr(frappe.local, "request", None) else None
			)
		return super().is_permitted()
