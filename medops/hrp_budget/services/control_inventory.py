"""Guarded release inventory for budget control; not authority to spend."""

from __future__ import annotations

import frappe

from medops.common.budget_control import ordered_budget_keys
from medops.common.budget_control_history import ReleaseInventoryEntry
from medops.common.budget_ledger import LEDGER, LEDGER_PUBLIC_FIELDS, RELEASE
from medops.common.budget_statement_access import native_ledger_base_check
from medops.common.exact_money import signed_decimal_amount
from medops.hrp_budget.services.budget_statement import (
	MAX_PRIVATE_PROOF_ENTRIES,
	MAX_STATEMENT_ENTRIES,
)
from medops.hrp_budget.services.consolidation_access import approved_department
from medops.hrp_budget.services.control_guards import assert_budget_guards, invalidate_budget_guards
from medops.hrp_budget.services.control_sources import resolve_budget_control_key
from medops.hrp_budget.services.ledger_integrity import complete_batch
from medops.hrp_budget.services.planning import native_document
from medops.hrp_budget.services.planning_access import READ_ROLES
from medops.hrp_budget.services.release_sources import stored_release_facts
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.data_access import require_scope, scope_allowed
from medops.services.errors import raise_ione_error

RELEASE_PARENT_PAGE_SIZE = 128


def posted_release_names_for_guarded_inventory(units):
	"""Visit every posted parent for the guarded hospitals in database order.

	Do not infer completeness from physical ledger rows: a whole missing batch
	still has to be discovered through its posted parent. This cursor operates
	under the common writer guard and READ COMMITTED; it does not authorize reads.
	"""
	seen = set()
	for company, hospital in sorted({(company, hospital) for company, hospital, _ in units}):
		cursor = None
		while True:
			filters = {"company": company, "hospital": hospital, "status": "Posted", "docstatus": 1}
			if cursor is not None:
				filters["name"] = [">", cursor]
			page = frappe.get_all(
				RELEASE,
				filters=filters,
				pluck="name",
				order_by="name asc",
				limit_page_length=RELEASE_PARENT_PAGE_SIZE,
			)
			if len(page) > RELEASE_PARENT_PAGE_SIZE:
				raise_ione_error("CONFIGURATION_INVALID")
			for name in page:
				if not isinstance(name, str) or not name or name in seen:
					raise_ione_error("CONFIGURATION_INVALID")
				seen.add(name)
				if len(seen) > MAX_PRIVATE_PROOF_ENTRIES:
					raise_ione_error("INVALID_REQUEST")
				yield name
			if len(page) < RELEASE_PARENT_PAGE_SIZE:
				break
			cursor = page[-1]


def _scope(key):
	return (
		key.company,
		key.hospital,
		key.organization_unit,
		key.budget_subject,
		key.funding_source,
		key.project,
	)


def _check_context(units, actor):
	state = frappe.db.get_value("User", actor, ["enabled", "user_type"], as_dict=True)
	if (
		frappe.session.user != actor
		or not state
		or not state.enabled
		or state.user_type != "System User"
		or not READ_ROLES.intersection(frappe.get_roles(actor))
		or not frappe.has_permission(LEDGER, "read", user=actor)
	):
		raise_ione_error("PERMISSION_DENIED")
	for company, hospital, unit in units:
		require_scope(LEDGER, company=company, hospital=hospital, organization_unit=unit)
		probe = frappe.get_doc(
			dict(doctype=LEDGER, company=company, hospital=hospital, organization_unit=unit)
		)
		if not can_read_named_fields(probe, set(LEDGER_PUBLIC_FIELDS), user=actor):
			raise_ione_error("PERMISSION_DENIED")


def _check_entry(row, references, actor):
	doc = frappe.get_doc(dict(row, doctype=LEDGER))
	with native_ledger_base_check():
		allowed = frappe.has_permission(LEDGER, "read", doc=doc, user=actor)
	if (
		not allowed
		or not scope_allowed(doc, user=actor)
		or not can_read_named_fields(doc, set(LEDGER_PUBLIC_FIELDS), user=actor)
	):
		raise_ione_error("PERMISSION_DENIED")
	ref = references.get(row.origin_plan)
	if ref is None:
		raise_ione_error("CONFIGURATION_INVALID")
	actual, snapshot = approved_department(ref.submission, ref.approval_case, user=actor)
	if actual != ref or snapshot.organization_unit != row.organization_unit:
		raise_ione_error("CONFIGURATION_INVALID")


def guarded_release_inventory(value: object) -> tuple[ReleaseInventoryEntry, ...]:
	"""Prove bounded release batches and physical rows for each guarded unit.

	This private candidate covers only the existing allocation ledger, across all
	periods, currencies and organization versions. It is not an available balance
	or posting authority: future event tables and all writers must share the guard.
	It must only run inside a READ COMMITTED transaction holding exactly these
	guards. Whole-transaction rollback is mandatory on every failure.
	"""
	from medops.hrp_budget.services.release_execution import refresh_financial_authority

	actor = frappe.session.user
	try:
		keys = ordered_budget_keys(value)
		assert_budget_guards(keys)
		refresh_financial_authority(actor)
		units = sorted({(key.company, key.hospital, key.organization_unit) for key in keys})
		scopes = {_scope(key) for key in keys}
		_check_context(units, actor)
		unit_set = set(units)
		rows = {}
		references = {}
		work = 0
		for name in posted_release_names_for_guarded_inventory(units):
			parent = frappe.get_doc(RELEASE, name)
			facts = stored_release_facts(parent)
			work += len(facts.snapshot.lines)
			if work > MAX_PRIVATE_PROOF_ENTRIES:
				raise_ione_error("INVALID_REQUEST")
			context = facts.snapshot.context
			if not any(
				(context.company, context.hospital, line.organization_unit) in unit_set
				for line in facts.snapshot.lines
			):
				continue
			_, batch, _ = complete_batch(parent, facts=facts)
			refs = {ref.origin_plan: ref for ref in facts.snapshot.sources}
			for row in batch:
				if (row.company, row.hospital, row.organization_unit) in unit_set:
					if row.name in rows or len(rows) >= MAX_STATEMENT_ENTRIES:
						raise_ione_error("CONFIGURATION_INVALID")
					_check_entry(row, refs, actor)
					rows[row.name] = row
					references[row.name] = refs
		physical = []
		for company, hospital, unit in units:
			physical.extend(
				frappe.get_all(
					LEDGER,
					filters={"company": company, "hospital": hospital, "organization_unit": unit},
					pluck="name",
					order_by="name asc",
					limit_page_length=MAX_STATEMENT_ENTRIES + 1,
				)
			)
			if len(physical) > MAX_STATEMENT_ENTRIES:
				raise_ione_error("INVALID_REQUEST")
		if len(physical) != len(set(physical)) or set(physical) != set(rows):
			raise_ione_error("CONFIGURATION_INVALID")
		entries = []
		currencies = {}
		accounts = {}
		centers = {}
		for row in rows.values():
			key = resolve_budget_control_key(
				{
					field: str(row.get(field)) if field in {"period_start", "period_end"} else row.get(field)
					for field in (
						"company",
						"hospital",
						"organization_unit",
						"fiscal_year",
						"budget_subject",
						"funding_source",
						"project",
						"period_start",
						"period_end",
					)
				}
			)
			if _scope(key) in scopes:
				precision = int(row.currency_precision)
				if row.currency not in currencies:
					currencies[row.currency] = native_document("Currency", row.currency, {"name"}).name
				if row.account not in accounts:
					accounts[row.account] = native_document("Account", row.account, {"name"}).name
				if row.cost_center not in centers:
					centers[row.cost_center] = native_document("Cost Center", row.cost_center, {"name"}).name
				entries.append(
					ReleaseInventoryEntry(
						row.name,
						key,
						currencies[row.currency],
						precision,
						signed_decimal_amount(row.amount, precision),
						accounts[row.account],
						centers[row.cost_center],
					)
				)
		if frappe.session.user != actor:
			raise_ione_error("CONFLICT")
		refresh_financial_authority(actor)
		_check_context(units, actor)
		for name, row in rows.items():
			_check_entry(row, references[name], actor)
		for name, resolved in currencies.items():
			if native_document("Currency", name, {"name"}).name != resolved:
				raise_ione_error("CONFLICT")
		for doctype, names in (("Account", accounts), ("Cost Center", centers)):
			for name, resolved in names.items():
				if native_document(doctype, name, {"name"}).name != resolved:
					raise_ione_error("CONFLICT")
		assert_budget_guards(keys)
		return tuple(sorted(entries, key=lambda entry: entry.name))
	except BaseException:
		invalidate_budget_guards()
		raise
