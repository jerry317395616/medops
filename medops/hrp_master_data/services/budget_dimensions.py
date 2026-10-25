from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import Any

import frappe

from medops.common.budget_planning import (
	DIMENSION,
	FUND,
	MASTER_TYPES,
	VALUE,
	MasterUpsert,
	master_identity,
	master_write,
)
from medops.common.domain_service import DomainServiceDefinition
from medops.hrp_workflow_authorization.services.rule_contracts import can_read_named_fields
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.data_access import (
	public_fields_readable,
	require_scope,
	scope_allowed,
	scope_permission_query,
)
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_authenticated_user, require_roles

WRITE_ROLES = frozenset({"System Manager", "HRP System Manager", "HRP Data Steward"})
READ_ROLES = WRITE_ROLES | {"HRP Budget User", "HRP Budget Manager", "HRP Finance User", "HRP Auditor"}
_query: ContextVar[bool] = ContextVar("medops_budget_master_query", default=False)


@contextmanager
def master_query_scope():
	token = _query.set(True)
	try:
		yield
	finally:
		_query.reset(token)


def query_condition(doctype: str, user=None) -> str:
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	return scope_permission_query(doctype, user) if _query.get() else "1=0"


def dimension_query(user=None):
	return query_condition(DIMENSION, user)


def value_query(user=None):
	return query_condition(VALUE, user)


def fund_query(user=None):
	return query_condition(FUND, user)


def native_read(doc, fields: set[str], *, user=None, export=False) -> bool:
	user = user or frappe.session.user
	return (
		frappe.has_permission(doc.doctype, "read", doc=doc, user=user)
		and (not export or frappe.has_permission(doc.doctype, "export", doc=doc, user=user))
		and can_read_named_fields(doc, fields, user=user)
	)


def master_permission(doc, user=None, ptype=None, debug=False):
	del debug
	user = user or frappe.session.user
	if ptype not in {None, "read", "select", "report", "export", "print"} or user == "Guest":
		return False
	export = ptype in {"export", "print"}
	if not READ_ROLES.intersection(frappe.get_roles(user)) or not scope_allowed(
		doc, user=user, action="export" if export else "read"
	):
		return False
	if not public_fields_readable(doc, user=user):
		return False
	for dt, name, fields in (
		("Company", doc.company, {"name"}),
		("HRP Hospital", doc.hospital, {"company"}),
	):
		if (
			not name
			or not frappe.db.exists(dt, name)
			or not native_read(frappe.get_doc(dt, name), fields, user=user, export=export)
		):
			return False
	if doc.doctype == VALUE:
		if not doc.dimension or not frappe.db.exists(DIMENSION, doc.dimension):
			return False
		parent = frappe.get_doc(DIMENSION, doc.dimension)
		if (
			parent.company != doc.company
			or parent.hospital != doc.hospital
			or not native_read(parent, {"company", "hospital", "purpose"}, user=user, export=export)
		):
			return False
	return True


def get_budget_master(doctype: str, name: str, *, lock=False):
	if doctype not in MASTER_TYPES.values():
		raise_ione_error("INVALID_REQUEST")
	if not frappe.db.exists(doctype, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	doc = frappe.get_doc(doctype, name, for_update=lock)
	if not frappe.has_permission(doctype, "read", doc=doc) or not public_fields_readable(doc):
		raise_ione_error("PERMISSION_DENIED")
	return doc


def require_effective(doc, start: str, end: str):
	if not doc.enabled or str(doc.valid_from) > start or (doc.valid_to and str(doc.valid_to) < end):
		raise_ione_error("INVALID_STATE_TRANSITION")


def canonical_master(command: MasterUpsert) -> MasterUpsert:
	if not frappe.db.exists("Company", command.company) or not frappe.db.exists(
		"HRP Hospital", command.hospital
	):
		raise_ione_error("RESOURCE_NOT_FOUND")
	company = frappe.get_doc("Company", command.company)
	hospital = frappe.get_doc("HRP Hospital", command.hospital)
	if not native_read(company, {"name"}) or not native_read(
		hospital, {"company", "enabled", "valid_from", "valid_to"}
	):
		raise_ione_error("PERMISSION_DENIED")
	if hospital.company != company.name:
		raise_ione_error("CONFLICT")
	dimension = get_budget_master(DIMENSION, command.dimension).name if command.dimension else None
	return replace(command, company=company.name, hospital=hospital.name, dimension=dimension)


class UpsertBudgetMaster(DomainService[MasterUpsert]):
	definition = DomainServiceDefinition(
		name="hrp_master_data.budget_dimensions.upsert", version=1, kind="command", required_roles=WRITE_ROLES
	)

	def request_payload(self, command):
		return command.as_request_payload()

	def validate(self, command):
		doctype = MASTER_TYPES[command.kind]
		require_scope(
			doctype,
			company=command.company,
			hospital=command.hospital,
			organization_unit=None,
			action="write" if command.expected_revision else "create",
		)
		name = master_identity(command)
		if frappe.db.exists(doctype, name):
			get_budget_master(doctype, name)
		if command.dimension:
			get_budget_master(DIMENSION, command.dimension)

	def authorize_result(self, result):
		get_budget_master(str(result["doctype"]), str(result["name"]))

	def perform(self, command):
		doctype = MASTER_TYPES[command.kind]
		hospital = frappe.get_doc("HRP Hospital", command.hospital, for_update=True)
		# A concurrent native company rename must not rekey a newly inserted
		# master after its identity was calculated from the old company name.
		if not frappe.db.get_value("Company", command.company, "name", for_update=True):
			raise_ione_error("RESOURCE_NOT_FOUND")
		company = frappe.get_doc("Company", command.company, for_update=True)
		if not native_read(company, {"name"}):
			raise_ione_error("PERMISSION_DENIED")
		if hospital.company != command.company or not native_read(
			hospital, {"company", "enabled", "valid_from", "valid_to"}
		):
			raise_ione_error("PERMISSION_DENIED")
		if command.enabled:
			if not hospital.enabled or (
				hospital.valid_from and str(hospital.valid_from) > command.valid_from
			):
				raise_ione_error("INVALID_STATE_TRANSITION")
			if hospital.valid_to and (not command.valid_to or command.valid_to > str(hospital.valid_to)):
				raise_ione_error("CONFLICT")
		if command.dimension:
			parent = get_budget_master(DIMENSION, command.dimension, lock=True)
			if parent.company != command.company or parent.hospital != command.hospital:
				raise_ione_error("CONFLICT")
			if command.enabled:
				require_effective(parent, command.valid_from, command.valid_to or "9999-12-31")
		name = master_identity(command)
		exists = frappe.db.get_value(doctype, name, "name", for_update=True)
		if bool(exists) != bool(command.expected_revision):
			raise_ione_error("CONFLICT")
		with master_write():
			if exists:
				doc = get_budget_master(doctype, name, lock=True)
				if int(doc.revision) != command.expected_revision:
					raise_ione_error("CONFLICT")
				doc.flags.locked_revision = int(doc.revision)
				for key, value in command.as_request_payload().items():
					if key not in {"kind", "expected_revision"} and doc.meta.has_field(key):
						doc.set(key, value)
				doc.save(ignore_permissions=True)
			else:
				doc = frappe.get_doc(
					{
						"doctype": doctype,
						"identity_key": name,
						**{
							key: value
							for key, value in command.as_request_payload().items()
							if key not in {"kind", "expected_revision"}
						},
					}
				).insert(ignore_permissions=True)
		emit_audit_event(
			"budget_master_saved",
			logger_name="medops.budget",
			master_type=command.kind,
			revision=int(doc.revision),
		)
		# A replay never returns stale copied values; the caller reloads through the safe reader.
		return {"doctype": doctype, "name": doc.name, "revision": int(doc.revision)}


def upsert_budget_master(command: MasterUpsert, *, idempotency_key=None, correlation_id=None):
	require_authenticated_user()
	require_roles(WRITE_ROLES)
	require_scope(
		MASTER_TYPES[command.kind],
		company=command.company,
		hospital=command.hospital,
		organization_unit=None,
		action="write" if command.expected_revision else "create",
	)
	canonical = canonical_master(command)
	execution = UpsertBudgetMaster().execute(
		canonical, idempotency_key=idempotency_key, correlation_id=correlation_id
	)
	return {
		**execution.result,
		"idempotency_replayed": execution.idempotency_replayed,
		"correlation_id": execution.correlation_id,
		"request_id": execution.request_id,
	}


def list_budget_masters(kind: str, filters: dict[str, Any], *, offset: int = 0, limit: int = 50, search=""):
	with service_audit_scope():
		require_authenticated_user()
		require_roles(READ_ROLES)
		if (
			kind not in MASTER_TYPES
			or not 0 <= offset <= 5000
			or not 1 <= limit <= 100
			or len(search) > 140
			or set(filters) - {"name", "company", "hospital", "dimension", "enabled"}
		):
			raise_ione_error("INVALID_REQUEST")
		if "dimension" in filters and kind != "value":
			raise_ione_error("INVALID_REQUEST")
		doctype = MASTER_TYPES[kind]
		with master_query_scope():
			names = frappe.get_list(
				doctype,
				filters=filters,
				pluck="name",
				order_by="code asc,name asc",
				limit=5001,
				or_filters={field: ["like", "%" + search + "%"] for field in ("name", "code", "display_name")}
				if search
				else None,
			)
		if len(names) > 5000:
			raise_ione_error("INVALID_REQUEST")
		visible = []
		for name in names:
			doc = frappe.get_doc(doctype, name)
			if frappe.has_permission(doctype, "read", doc=doc) and public_fields_readable(doc):
				visible.append(doc.as_public_dict())
		page = visible[offset : offset + limit]
		return {
			"items": page,
			"next_offset": offset + limit if len(visible) > offset + limit else None,
			"limit": limit,
		}
