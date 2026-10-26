"""Current complete-source visibility for the financial operator's directory."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass

import frappe

from medops.common.budget_approval import SUBMISSION
from medops.common.budget_ledger import RELEASE, RELEASE_PUBLIC_FIELDS
from medops.common.budget_release_directory import (
	CONTEXT_FIELDS,
	MAX_DIRECTORY_CANDIDATES,
	SOURCE_PUBLIC_FIELDS,
	ReleaseDirectoryRequest,
	directory_digest,
)
from medops.common.budget_workflow import CASE
from medops.common.domain_service import DomainServiceDefinition
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services.access_graph import access_graph
from medops.hrp_budget.services.approval_access import case_query_scope
from medops.hrp_budget.services.ledger_access import statement_actor
from medops.hrp_budget.services.planning_access import native_source
from medops.hrp_budget.services.release_access import (
	RELEASE_READ_ROLES,
	get_release,
	release_permission,
	release_query_scope,
)
from medops.hrp_budget.services.release_sources import approved_release_source
from medops.hrp_budget.services.statement_authority import refresh_statement_actor
from medops.hrp_budget.services.statement_cache import read_statement_document
from medops.hrp_budget.services.statement_permissions import (
	statement_metadata_types,
	statement_permission_stamp,
)
from medops.hrp_budget.services.submission_access import submission_query_scope
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.audit_context import emit_audit_event
from medops.services.data_access import require_scope
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_roles

_MISSING = object()


def directory_actor():
	actor = statement_actor()
	require_roles(RELEASE_READ_ROLES)
	return actor


@contextmanager
def isolated_visibility_messages():
	# A permission predicate can call a guarded source that raises internally.
	# Skipping that candidate must not leave its error body/header in a good list.
	keys = ("response", "response_headers", "message_log")
	saved = {key: getattr(frappe.local, key, _MISSING) for key in keys}
	frappe.local.response = frappe._dict()
	frappe.local.response_headers = {}
	frappe.local.message_log = []
	try:
		yield
	finally:
		for key, value in saved.items():
			if value is _MISSING:
				if hasattr(frappe.local, key):
					delattr(frappe.local, key)
			else:
				setattr(frappe.local, key, value)


def visible(check):
	failure = None
	with isolated_visibility_messages():
		try:
			return check()
		except IoneApplicationError as error:
			if error.code == "IONE-CORE-0002":
				return None
			failure = error.code
	if failure:
		raise_ione_error(failure)
	return None


def public_release(doc):
	return json.loads(frappe.as_json({key: doc.get(key) for key in RELEASE_PUBLIC_FIELDS}))


def directory_context(filters, kind):
	actor = directory_actor()
	company = native_source("Company", filters["company"], {"name"}, user=actor)
	hospital = native_source("HRP Hospital", filters["hospital"], {"company"}, user=actor)
	year = native_source("Fiscal Year", filters["fiscal_year"], {"companies.company"}, user=actor)
	if company is None or hospital is None or year is None:
		raise_ione_error("PERMISSION_DENIED")
	if hospital.company != company.name or (
		year.companies and company.name not in {row.company for row in year.companies}
	):
		raise_ione_error("CONFLICT")
	if filters.get("organization_unit"):
		unit = native_source(
			"HRP Organization Unit", filters["organization_unit"], {"company", "hospital"}, user=actor
		)
		if unit is None:
			raise_ione_error("PERMISSION_DENIED")
		if unit.company != company.name or unit.hospital != hospital.name:
			raise_ione_error("CONFLICT")
	if (
		filters.get("currency")
		and native_source("Currency", filters["currency"], {"name"}, user=actor) is None
	):
		raise_ione_error("PERMISSION_DENIED")
	require_scope(
		RELEASE,
		company=company.name,
		hospital=hospital.name,
		organization_unit=filters.get("organization_unit"),
	)
	doctype = RELEASE if kind == "records" else CASE
	fields = set(RELEASE_PUBLIC_FIELDS if kind == "records" else SOURCE_PUBLIC_FIELDS)
	if kind == "eligible":
		# Computed from the checked consolidated snapshot, not a case field.
		fields.remove("line_count")
	probe = frappe.get_doc(
		dict(
			doctype=doctype,
			**{key: filters[key] for key in CONTEXT_FIELDS},
			organization_unit=filters.get("organization_unit"),
		)
	)
	if not frappe.has_permission(doctype, "read", user=actor) or not can_read_named_fields(
		probe, fields, user=actor
	):
		raise_ione_error("PERMISSION_DENIED")
	return {key: filters[key] for key in CONTEXT_FIELDS} | {
		"organization_unit": filters.get("organization_unit")
	}


def bounded_names(doctype, filters, order_by):
	names = frappe.get_list(
		doctype,
		filters=filters,
		pluck="name",
		order_by=order_by,
		limit_page_length=MAX_DIRECTORY_CANDIDATES + 1,
	)
	if len(names) > MAX_DIRECTORY_CANDIDATES:
		raise_ione_error("INVALID_REQUEST")
	return tuple(names)


def directory_candidates(command):
	directory_actor()
	filters = dict(command.filters)
	require_scope(
		RELEASE,
		company=filters["company"],
		hospital=filters["hospital"],
		organization_unit=filters.get("organization_unit"),
	)
	if command.kind == "records":
		with release_query_scope():
			return bounded_names(RELEASE, filters, "prepared_at desc,name asc")
	# Start with native, scoped consolidated sources so approved department
	# cases cannot become financial release proposals.
	source_filters = {key: value for key, value in filters.items() if key != "name"}
	with submission_query_scope():
		sources = bounded_names(
			SUBMISSION,
			dict(source_filters, submission_type="Consolidated", status="Sealed", docstatus=1),
			"name asc",
		)
	if not sources:
		return ()
	consumed = frappe.get_all(
		RELEASE,
		filters={key: filters[key] for key in CONTEXT_FIELDS} | {"operation": "Release"},
		pluck="source_approval",
		limit_page_length=MAX_DIRECTORY_CANDIDATES + 1,
	)
	if len(consumed) > MAX_DIRECTORY_CANDIDATES:
		raise_ione_error("INVALID_REQUEST")
	if filters.get("name") in consumed:
		return ()
	case_filters = dict(filters, submission=["in", list(sources)], status="Approved", docstatus=1)
	if consumed and "name" not in case_filters:
		case_filters["name"] = ["not in", list(consumed)]
	with case_query_scope():
		return bounded_names(
			CASE,
			case_filters,
			"completed_at desc,name asc",
		)


def collect_release(name):
	doc = read_statement_document(RELEASE, name)
	if not frappe.has_permission(RELEASE, "read", doc=doc) or not release_permission(doc, ptype="read"):
		return None
	return public_release(doc)


def collect_source(name):
	case, source, snapshot, _ = approved_release_source(name)
	# A unique prepared intent already consumes this source. No private
	# existing record identifier or status is exposed by this eligibility test.
	if frappe.db.exists(RELEASE, {"operation": "Release", "source_approval": name}):
		return None
	return dict(
		name=case.name,
		revision=int(case.revision),
		subject=case.subject,
		company=case.company,
		hospital=case.hospital,
		organization_unit=case.organization_unit,
		fiscal_year=case.fiscal_year,
		currency=case.currency,
		currency_precision=int(case.currency_precision),
		total_amount=case.total_amount,
		line_count=len(snapshot.lines),
		submission=source.name,
	)


def collect_directory(command, names):
	items = []
	with access_graph():
		for name in names:
			item = visible(
				lambda name=name: collect_release(name) if command.kind == "records" else collect_source(name)
			)
			if item is not None:
				items.append(item)
	return items


class ReadReleaseDirectory(DomainService[ReleaseDirectoryRequest]):
	definition = DomainServiceDefinition(
		name="hrp_budget.release.directory", version=1, kind="query", required_roles=RELEASE_READ_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		directory_actor()

	def request_payload(self, command):
		return asdict(command)

	def perform(self, command):
		self.command = command
		self.context = directory_context(dict(command.filters), command.kind)
		self.metadata = statement_metadata_types()
		self.stamp = statement_permission_stamp(self.metadata)
		self.names = directory_candidates(command)
		self.items = collect_directory(command, self.names)
		digest = directory_digest(command, self.items)
		if command.expected_digest and command.expected_digest != digest:
			raise_ione_error("CONFLICT")
		return dict(
			items=self.items[command.offset : command.offset + command.limit],
			offset=command.offset,
			limit=command.limit,
			next_offset=command.offset + command.limit
			if len(self.items) > command.offset + command.limit
			else None,
			list_digest=digest,
		)

	def authorize_result(self, result):
		refresh_statement_actor()
		if directory_context(dict(self.command.filters), self.command.kind) != self.context:
			raise_ione_error("CONFLICT")
		if (
			directory_candidates(self.command) != self.names
			or collect_directory(self.command, self.names) != self.items
			or statement_permission_stamp(self.metadata) != self.stamp
		):
			raise_ione_error("CONFLICT")
		emit_audit_event(
			"budget_release_directory_read",
			logger_name="medops.budget",
			line_count=len(self.items[self.command.offset : self.command.offset + self.command.limit]),
		)


@dataclass(frozen=True, slots=True)
class ReleaseRecordRequest:
	name: str


class ReadReleaseRecord(DomainService[ReleaseRecordRequest]):
	definition = DomainServiceDefinition(
		name="hrp_budget.release.record", version=1, kind="query", required_roles=RELEASE_READ_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		directory_actor()

	def request_payload(self, command):
		return asdict(command)

	def perform(self, command):
		self.command = command
		self.metadata = statement_metadata_types()
		self.stamp = statement_permission_stamp(self.metadata)
		return public_release(get_release(command.name))

	def authorize_result(self, result):
		refresh_statement_actor()
		if (
			public_release(get_release(self.command.name)) != result
			or statement_permission_stamp(self.metadata) != self.stamp
		):
			raise_ione_error("CONFLICT")
