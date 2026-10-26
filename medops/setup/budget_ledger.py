from __future__ import annotations

import frappe

from medops.common.budget_ledger import BUDGET_LEDGER_DEFINITION, LEDGER, RELEASE, RELEASE_ENTRY
from medops.common.integration_outbox import OUTBOX, OUTBOX_DEFINITION
from medops.services.immutable_ledger import validate_immutable_ledger_doctype
from medops.services.transactional_message import validate_message_box_doctype


def ensure_budget_ledger():
	validate_immutable_ledger_doctype(BUDGET_LEDGER_DEFINITION)
	validate_message_box_doctype(OUTBOX_DEFINITION)
	for doctype, fields, name in (
		(RELEASE, ["company", "hospital", "fiscal_year", "status"], "idx_hrp_budget_release_scope"),
		(RELEASE, ["source_approval", "operation"], "idx_hrp_budget_release_source"),
		(RELEASE_ENTRY, ["release", "event_kind", "release_revision"], "idx_hrp_budget_release_evidence"),
		(RELEASE_ENTRY, ["actor", "event_kind", "expires_at"], "idx_hrp_budget_release_actor"),
		(
			LEDGER,
			["company", "hospital", "organization_unit", "fiscal_year", "currency"],
			"idx_hrp_budget_ledger_statement",
		),
		(LEDGER, ["voucher_no", "entry_type"], "idx_hrp_budget_ledger_voucher"),
		(OUTBOX, ["event_type", "status", "available_at"], "idx_hrp_integration_outbox_due"),
		(OUTBOX, ["status", "lease_expires_at"], "idx_hrp_integration_outbox_lease"),
	):
		frappe.db.add_index(doctype, [f"`{field}`" for field in fields], name)
	return {"schema_version": 1, "runtime_models": 4, "financial_seeds": 0}
