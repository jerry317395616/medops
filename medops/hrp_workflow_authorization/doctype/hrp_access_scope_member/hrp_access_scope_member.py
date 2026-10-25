from __future__ import annotations

from typing import TYPE_CHECKING

from frappe.model.document import Document


class HRPAccessScopeMember(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		allow_cancel: DF.Check
		allow_create: DF.Check
		allow_export: DF.Check
		allow_read: DF.Check
		allow_submit: DF.Check
		allow_write: DF.Check
		enabled: DF.Check
		target_doctype: DF.Link | None
		user: DF.Link
	# end: auto-generated types


__all__ = ["HRPAccessScopeMember"]
