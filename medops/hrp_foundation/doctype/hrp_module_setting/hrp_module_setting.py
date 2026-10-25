from __future__ import annotations

import frappe
from frappe.model.document import Document

from medops.common.constants import APP_NAME
from medops.services.errors import raise_ione_error
from medops.services.module_registry import load_module_registry


class HRPModuleSetting(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		description: DF.SmallText | None
		domain_group: DF.Data
		enabled: DF.Check
		label_cn: DF.Data | None
		module_key: DF.Data | None
		module_name: DF.Link
		sequence: DF.Int
		workspace_route: DF.Data | None
	# end: auto-generated types

	def validate(self) -> None:
		app_name = frappe.db.get_value("Module Def", self.module_name, "app_name")
		if app_name != APP_NAME:
			raise_ione_error("INVALID_REQUEST")
		modules = {row.module: row for row in load_module_registry().modules}
		if self.module_name not in modules:
			raise_ione_error("INVALID_REQUEST")
		if self.module_key != modules[self.module_name].package:
			raise_ione_error("INVALID_REQUEST")
