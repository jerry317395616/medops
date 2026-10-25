from __future__ import annotations

import frappe

from medops.setup.modules import declared_modules


def daily_maintenance() -> None:
	"""Low-cost daily consistency check; business jobs belong in their owning modules."""
	if not frappe.db.exists("DocType", "HRP Module Setting"):
		return
	declared = set(declared_modules())
	configured = set(frappe.get_all("HRP Module Setting", pluck="module_name"))
	missing = sorted(declared - configured)
	if missing:
		frappe.log_error(
			title="HRP module settings missing",
			message="Missing settings: " + ", ".join(missing),
		)
