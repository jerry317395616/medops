from __future__ import annotations

from typing import Any, cast

import frappe

from medops.common.core_reports import (
	REFERENCE,
	reference_batch,
	reference_identity,
	reference_write,
)
from medops.common.domain_service import DomainServiceDefinition
from medops.hrp_master_data.services.access import require_target_read, target_readable
from medops.hrp_master_data.services.external_code_mapping import (
	assert_master_data_hospital_scope,
	get_operational_master_data_domain,
)
from medops.services.audit_context import emit_audit_event
from medops.services.data_access import (
	public_fields_readable,
	require_scope,
	scope_allowed,
	scope_permission_query,
)
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error

WRITE_ROLES = frozenset({"System Manager", "HRP System Manager", "HRP Data Steward"})
READ_ROLES = WRITE_ROLES | {"HRP User", "HRP Auditor", "HRP Integration User"}


def reference_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	# Names are obtained by the reviewed facade, never by an unchecked SQL list.
	from medops.services.core_report_access import scoped_query_active

	return scope_permission_query(REFERENCE, user) if scoped_query_active() else "1=0"


def reference_permission(doc, user=None, ptype=None, debug=False) -> bool:
	del debug
	user = user or frappe.session.user
	if ptype not in {None, "read", "select", "export", "print", "report"} or user == "Guest":
		return False
	action = "export" if ptype in {"export", "print"} else "read"
	return (
		bool(READ_ROLES.intersection(frappe.get_roles(user)))
		and scope_allowed(doc, user=user, action=action)
		and public_fields_readable(doc, user=user)
		and target_readable(
			doc.target_doctype, doc.target_name, company=doc.company, user=user, action=action
		)
	)


def visible_references(filters: dict[str, Any], *, export: bool = False) -> list[Any]:
	from medops.services.core_report_access import visible_documents

	return visible_documents(REFERENCE, filters, export=export)


class RegisterReferences(DomainService[dict[str, Any]]):
	definition = DomainServiceDefinition(
		name="hrp_foundation.references.register", version=1, kind="command", required_roles=WRITE_ROLES
	)

	def request_payload(self, command):
		return command

	def validate(self, command):
		context = command["scope"]
		domain = None
		for row in command["entries"]:
			require_scope(
				REFERENCE,
				company=context["company"],
				hospital=context["hospital"],
				organization_unit=context["organization_unit"],
				action="write" if row["expected_revision"] else "create",
			)
			domain, _ = get_operational_master_data_domain(context["master_data_domain"], lock=False)
			require_target_read(domain.target_doctype, row["target_name"], company=context["company"])
		if domain is None:
			raise_ione_error("INVALID_REQUEST")
		context["master_data_domain"] = domain.name
		for key, dt in (
			("company", "Company"),
			("hospital", "HRP Hospital"),
			("organization_unit", "HRP Organization Unit"),
		):
			if context.get(key):
				canonical = frappe.db.get_value(dt, context[key], "name")
				if not canonical:
					raise_ione_error("RESOURCE_NOT_FOUND")
				context[key] = canonical
		seen = set()
		for row in command["entries"]:
			row["target_name"] = frappe.db.get_value(domain.target_doctype, row["target_name"], "name")
			if row["target_name"] in seen:
				raise_ione_error("INVALID_REQUEST")
			seen.add(row["target_name"])
			identity = reference_identity({**context, **row, "target_doctype": domain.target_doctype})
			if frappe.db.exists(REFERENCE, identity) and not frappe.has_permission(
				REFERENCE, "read", doc=frappe.get_doc(REFERENCE, identity)
			):
				raise_ione_error("PERMISSION_DENIED")
		command["entries"].sort(key=lambda row: row["target_name"])

	def authorize_result(self, result):
		for row in cast(list[dict[str, Any]], result["items"]):
			doc = frappe.get_doc(REFERENCE, row["name"])
			if not frappe.has_permission(REFERENCE, "read", doc=doc):
				raise_ione_error("PERMISSION_DENIED")

	def perform(self, command):
		context = command["scope"]
		domain, _ = get_operational_master_data_domain(context["master_data_domain"], lock=True)
		items = []
		for row in command["entries"]:
			assert_master_data_hospital_scope(
				company=context["company"],
				hospital=context["hospital"],
				organization_unit=context["organization_unit"],
				effective_on=row["valid_from"],
			)
			require_target_read(
				domain.target_doctype, row["target_name"], company=context["company"], lock=True
			)
			values = {**context, **row, "target_doctype": domain.target_doctype}
			identity = reference_identity(values)
			exists = frappe.db.exists(REFERENCE, identity)
			if bool(exists) != bool(row["expected_revision"]):
				raise_ione_error("CONFLICT")
			with reference_write():
				if exists:
					doc = frappe.get_doc(REFERENCE, identity, for_update=True)
					if not frappe.has_permission(REFERENCE, "read", doc=doc):
						raise_ione_error("PERMISSION_DENIED")
					if int(doc.revision) != row["expected_revision"]:
						raise_ione_error("CONFLICT")
					changed = any(
						doc.get(key) != values[key]
						if key == "enabled"
						else str(doc.get(key) or "") != str(values[key] or "")
						for key in ("enabled", "valid_from", "valid_to")
					)
					if changed:
						for key in ("enabled", "valid_from", "valid_to"):
							doc.set(key, values[key])
						doc.flags.locked_revision = int(doc.revision)
						doc.save(ignore_permissions=True)
				else:
					doc = frappe.get_doc(
						{
							"doctype": REFERENCE,
							**{k: v for k, v in values.items() if k != "expected_revision"},
							"identity_key": identity,
							"revision": 1,
						}
					).insert(ignore_permissions=True)
			items.append(doc.as_public_dict())
		emit_audit_event(
			"hospital_master_population_registered", logger_name="medops.references", count=len(items)
		)
		return {"items": items}


def register_references(scope: object, entries: object, *, idempotency_key=None, correlation_id=None):
	try:
		command = reference_batch(scope, entries)
	except (ValueError, TypeError) as exc:
		raise_ione_error("INVALID_REQUEST", cause=exc)
	result = RegisterReferences().execute(
		command, idempotency_key=idempotency_key, correlation_id=correlation_id
	)
	return {
		**result.result,
		"idempotency_replayed": result.idempotency_replayed,
		"correlation_id": result.correlation_id,
	}


def scheduled_population(rule, after_name: str | None, limit: int):
	filters = {
		"master_data_domain": rule.master_data_domain,
		"company": rule.company,
		"hospital": rule.hospital,
		"enabled": 1,
	}
	if rule.organization_unit:
		filters["organization_unit"] = rule.organization_unit
	from frappe.utils import today

	rows = visible_references(filters)
	names = sorted(
		{
			doc.target_name
			for doc in rows
			if str(doc.valid_from) <= today()
			and (not doc.valid_to or str(doc.valid_to) >= today())
			and (not after_name or doc.target_name > after_name)
		}
	)
	return [frappe._dict(name=name) for name in names[:limit]]
