from __future__ import annotations

from datetime import date

import frappe

from erpnext.setup.setup_wizard.setup_wizard import setup_complete

from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.environment import get_environment_status
from medops.services.errors import raise_ione_error

SYNTHETIC_COMPANY_NAME = "MedOps Synthetic Demonstration Hospital"
SYNTHETIC_COMPANY_ABBR = "IOHD"


def setup_synthetic_demo() -> dict[str, object]:
	with service_audit_scope():
		return _setup_synthetic_demo()


def _setup_synthetic_demo() -> dict[str, object]:
	status = get_environment_status()
	if status.get("name") != "demo" or not status.get("synthetic_data_only"):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	existing_companies = frappe.get_all("Company", pluck="name")
	if existing_companies:
		if existing_companies == [SYNTHETIC_COMPANY_NAME]:
			return {
				"status": "ok",
				"company": SYNTHETIC_COMPANY_NAME,
				"changed": False,
			}
		raise_ione_error("CONFLICT")

	year = date.today().year
	setup_complete(
		frappe._dict(
			{
				"country": "China",
				"currency": "CNY",
				"language": "zh",
				"timezone": "Asia/Shanghai",
				"company_name": SYNTHETIC_COMPANY_NAME,
				"company_abbr": SYNTHETIC_COMPANY_ABBR,
				"fy_start_date": f"{year}-01-01",
				"fy_end_date": f"{year}-12-31",
				"chart_of_accounts": "Standard",
				"domain": "Services",
				"bank_account": "I-ONE Synthetic Demo Bank",
			}
		)
	)
	emit_audit_event(
		"synthetic_demo_baseline_created",
		logger_name="medops.environment",
		environment="demo",
		changed=True,
	)
	return {
		"status": "ok",
		"company": SYNTHETIC_COMPANY_NAME,
		"changed": True,
	}
