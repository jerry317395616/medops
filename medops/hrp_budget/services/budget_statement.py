"""Complete original-unit books: prove all sources before paging any rows."""

from __future__ import annotations

from dataclasses import asdict

import frappe

from medops.common.budget_ledger import LEDGER, LEDGER_PUBLIC_FIELDS, RELEASE
from medops.common.budget_planning import FUND, VALUE
from medops.common.budget_statement import (
	MAX_STATEMENT_ENTRIES,
	STATEMENT_SCHEMA_VERSION,
	StatementRequest,
	complete_statement,
	statement_dates,
	with_display_labels,
)
from medops.common.domain_service import DomainServiceDefinition
from medops.common.organization import normalize_required_text
from medops.hrp_budget.services.access_graph import access_graph
from medops.hrp_budget.services.ledger_access import authorize_entry, public_entry, statement_actor
from medops.hrp_budget.services.ledger_integrity import complete_batch
from medops.hrp_budget.services.planning_access import READ_ROLES, native_source
from medops.hrp_budget.services.release_sources import stored_release_facts
from medops.hrp_budget.services.statement_authority import refresh_statement_actor
from medops.hrp_budget.services.statement_permissions import (
	statement_metadata_types,
	statement_permission_stamp,
)
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.audit_context import emit_audit_event
from medops.services.data_access import require_scope
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error

STATEMENT_PARENT_PAGE_SIZE = 128
MAX_PRIVATE_PROOF_ENTRIES = 1000000


def statement_display_labels(rows, context):
	"""Return names only for the current page and currently readable fields."""
	actor = statement_actor()
	result = []
	for row in rows:
		labels = {}
		for key, doctype in (("budget_subject", VALUE), ("funding_source", FUND)):
			doc = native_source(doctype, row[key], {"company", "hospital", "display_name"}, user=actor)
			label = None
			if doc is not None and doc.company == context["company"] and doc.hospital == context["hospital"]:
				try:
					label = normalize_required_text(doc.display_name, label="display_name", maximum=140)
				except ValueError:
					pass
			labels[key] = label
		result.append(labels)
	return result


def statement_context(query, *, export=False):
	actor = statement_actor()
	unit = native_source(
		"HRP Organization Unit",
		query.organization_unit,
		{"company", "hospital", "organization_version", "display_name"},
		user=actor,
		export=export,
	)
	year = native_source(
		"Fiscal Year",
		query.fiscal_year,
		{"year_start_date", "year_end_date", "companies.company"},
		user=actor,
		export=export,
	)
	currency = native_source("Currency", query.currency, {"name"}, user=actor, export=export)
	if unit is None or year is None or currency is None:
		raise_ione_error("PERMISSION_DENIED")
	company = native_source("Company", unit.company, {"name"}, user=actor, export=export)
	hospital = native_source("HRP Hospital", unit.hospital, {"company"}, user=actor, export=export)
	version = native_source(
		"HRP Organization Version",
		unit.organization_version,
		{"company", "hospital", "status"},
		user=actor,
		export=export,
	)
	if company is None or hospital is None or version is None:
		raise_ione_error("PERMISSION_DENIED")
	if (
		hospital.company != company.name
		or version.company != company.name
		or version.hospital != hospital.name
		or version.status != "Published"
		or int(version.docstatus) != 1
	):
		raise_ione_error("CONFIGURATION_INVALID")
	if year.companies and company.name not in {row.company for row in year.companies}:
		raise_ione_error("CONFLICT")
	try:
		first, last = statement_dates(query, str(year.year_start_date), str(year.year_end_date))
	except ValueError as error:
		raise_ione_error("INVALID_REQUEST", cause=error)
	context = dict(
		company=company.name,
		hospital=hospital.name,
		organization_version=version.name,
		organization_unit=unit.name,
		organization_unit_label=unit.display_name,
		fiscal_year=year.name,
		currency=currency.name,
		from_date=first,
		to_date=last,
		fiscal_year_start=str(year.year_start_date),
		fiscal_year_end=str(year.year_end_date),
	)
	require_statement_scope(context, export=export)
	probe = frappe.get_doc(dict(doctype=LEDGER, **context))
	if (
		not frappe.has_permission(LEDGER, "read", user=actor)
		or (export and not frappe.has_permission(LEDGER, "export", user=actor))
		or not can_read_named_fields(probe, set(LEDGER_PUBLIC_FIELDS), user=actor)
	):
		raise_ione_error("PERMISSION_DENIED")
	return context


def require_statement_scope(context, *, export=False):
	require_scope(
		LEDGER,
		company=context["company"],
		hospital=context["hospital"],
		organization_unit=context["organization_unit"],
		action="export" if export else "read",
	)


def context_filters(context):
	return {
		key: context[key]
		for key in ("company", "hospital", "organization_version", "fiscal_year", "currency")
	}


def statement_batches(context, *, export=False):
	# Private provenance enumeration deliberately does not filter by the
	# aggregate header's unit or by visible ledger rows. Either would silently
	# lose a deleted/misdimensioned original-leaf row. No header enters output.
	require_statement_scope(context, export=export)
	filters = dict(
		context_filters(context),
		status="Posted",
		docstatus=1,
		posting_date=["between", [context["fiscal_year_start"], context["to_date"]]],
	)
	names = []
	seen = set()
	while True:
		page = frappe.get_all(
			RELEASE,
			filters=filters,
			pluck="name",
			order_by="name asc",
			limit_page_length=STATEMENT_PARENT_PAGE_SIZE,
		)
		if len(page) > STATEMENT_PARENT_PAGE_SIZE:
			raise_ione_error("CONFIGURATION_INVALID")
		for name in page:
			if not isinstance(name, str) or not name or name in seen:
				raise_ione_error("CONFIGURATION_INVALID")
			seen.add(name)
			if len(seen) > MAX_PRIVATE_PROOF_ENTRIES:
				raise_ione_error("INVALID_REQUEST")
			names.append(name)
		if len(page) < STATEMENT_PARENT_PAGE_SIZE:
			break
		filters["name"] = [">", page[-1]]
	return tuple(names)


def physical_statement_entries(context, *, export=False):
	require_statement_scope(context, export=export)
	names = frappe.get_all(
		LEDGER,
		filters=dict(
			context_filters(context),
			organization_unit=context["organization_unit"],
			posting_date=["between", [context["fiscal_year_start"], context["to_date"]]],
		),
		pluck="name",
		order_by="name asc",
		limit_page_length=MAX_STATEMENT_ENTRIES + 1,
	)
	if len(names) > MAX_STATEMENT_ENTRIES:
		raise_ione_error("INVALID_REQUEST")
	return tuple(names)


class ReadBudgetStatement(DomainService[StatementRequest]):
	definition = DomainServiceDefinition(
		name="hrp_budget.ledger.statement", version=1, kind="query", required_roles=READ_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		statement_actor()

	def request_payload(self, command):
		return asdict(command)

	def perform(self, command):
		self.command = command
		self.context = statement_context(command.query, export=command.export)
		self.metadata_types = statement_metadata_types()
		self.authority_stamp = statement_permission_stamp(self.metadata_types)
		self.batches = statement_batches(self.context, export=command.export)
		self.visible = []
		self.references = {}
		work = 0
		with access_graph():
			for name in self.batches:
				parent = frappe.get_doc(RELEASE, name)
				facts = stored_release_facts(parent)
				work += len(facts.snapshot.lines)
				if work > MAX_PRIVATE_PROOF_ENTRIES:
					raise_ione_error("INVALID_REQUEST")
				if not any(
					line.organization_unit == self.context["organization_unit"]
					for line in facts.snapshot.lines
				):
					continue
				_, rows, _ = complete_batch(parent, facts=facts)
				refs = {ref.origin_plan: ref for ref in facts.snapshot.sources}
				for row in rows:
					if row.organization_unit != self.context["organization_unit"]:
						continue
					if any(
						str(row.get(key)) != str(self.context[key]) for key in context_filters(self.context)
					):
						raise_ione_error("CONFIGURATION_INVALID")
					authorize_entry(row, refs, export=command.export)
					self.visible.append(row)
					self.references[row.name] = refs[row.origin_plan]
					if len(self.visible) > MAX_STATEMENT_ENTRIES:
						raise_ione_error("INVALID_REQUEST")
		self.physical = physical_statement_entries(self.context, export=command.export)
		if set(self.physical) != {row.name for row in self.visible}:
			raise_ione_error("CONFIGURATION_INVALID")
		self.book = complete_statement(self.context, [public_entry(row) for row in self.visible])
		if command.expected_digest and command.expected_digest != self.book.digest:
			raise_ione_error("CONFLICT")
		result: dict[str, object] = dict(
			schema_version=STATEMENT_SCHEMA_VERSION,
			context=dict(self.book.context),
			statement_digest=self.book.digest,
			total_rows=len(self.book.rows),
			totals=dict(self.book.totals),
			offset=command.offset,
			limit=command.limit,
			next_offset=command.offset + command.limit
			if len(self.book.rows) > command.offset + command.limit
			else None,
			columns=[*LEDGER_PUBLIC_FIELDS, "running_balance"],
			rows=[]
			if command.export
			else [dict(row) for row in self.book.rows[command.offset : command.offset + command.limit]],
		)
		self.label_rows = (
			() if command.export else self.book.rows[command.offset : command.offset + command.limit]
		)
		labels = []
		if self.label_rows:
			with access_graph():
				labels = statement_display_labels(self.label_rows, self.context)
		result = with_display_labels(result, labels)
		self.display_labels = result.get("display_labels")
		return result

	def authorize_result(self, result):
		refresh_statement_actor()
		if statement_context(self.command.query, export=self.command.export) != self.context:
			raise_ione_error("CONFLICT")
		with access_graph():
			for row in self.visible:
				authorize_entry(row, {row.origin_plan: self.references[row.name]}, export=self.command.export)
			if (
				self.display_labels is not None
				and statement_display_labels(self.label_rows, self.context) != self.display_labels
			):
				raise_ione_error("CONFLICT")
		if (
			statement_batches(self.context, export=self.command.export) != self.batches
			or physical_statement_entries(self.context, export=self.command.export) != self.physical
			or statement_permission_stamp(self.metadata_types) != self.authority_stamp
		):
			raise_ione_error("CONFLICT")
		emit_audit_event(
			"budget_statement_exported" if self.command.export else "budget_statement_read",
			logger_name="medops.budget",
			line_count=len(self.book.rows),
		)
