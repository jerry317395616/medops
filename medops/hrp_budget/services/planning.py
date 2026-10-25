from __future__ import annotations

from dataclasses import replace
from datetime import date
from itertools import pairwise
from typing import Any

import frappe
from frappe.model.meta import get_field_precision
from frappe.utils import getdate, now_datetime, today

from medops.common.budget_planning import (
	DIMENSION,
	FUND,
	MAX_LINES,
	MAX_PLANS,
	PLAN,
	VALUE,
	VERSION,
	PlanLine,
	PlanningAction,
	PlanSave,
	VersionSave,
	allocate_amount,
	decimal_amount,
	line_identity,
	line_sort_key,
	plan_definition,
	planning_digest,
	planning_write,
	version_definition,
)
from medops.common.domain_service import DomainServiceDefinition, fingerprint_json
from medops.hrp_budget.services.planning_access import (
	MANAGER_ROLES,
	READ_ROLES,
	WRITE_ROLES,
	get_plan,
	get_version,
	plan_permission,
	planning_query_scope,
	require_planning_scope,
	version_permission,
)
from medops.hrp_master_data.services.budget_dimensions import (
	native_read,
	require_effective,
)
from medops.hrp_master_data.services.external_code_mapping import assert_master_data_hospital_scope
from medops.hrp_workflow_authorization.services.business_rules import enforce_business_rules
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.data_access import require_scope
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_authenticated_user, require_roles


def native_document(doctype: str, name: str, fields: set[str], *, lock=False, cache=None):
	key = (doctype, name)
	if cache is not None and key in cache:
		return cache[key]
	if not frappe.db.get_value(doctype, name, "name", for_update=lock):
		raise_ione_error("RESOURCE_NOT_FOUND")
	doc = frappe.get_doc(doctype, name, for_update=lock)
	if not native_read(doc, fields):
		raise_ione_error("PERMISSION_DENIED")
	if cache is not None:
		cache[key] = doc
	return doc


def periods_for(version) -> list[dict[str, Any]]:
	# The standard controller supplies the fiscal calendar boundaries. Its first
	# bucket starts at a calendar-month boundary; clamp a short/mid-month fiscal
	# year so no planning row can fall outside the real fiscal year.
	budget = frappe.get_doc(
		{
			"doctype": "Budget",
			"budget_start_date": version.period_start,
			"budget_end_date": version.period_end,
			"distribution_frequency": version.distribution_frequency,
		}
	)
	start, end = getdate(version.period_start), getdate(version.period_end)
	if end < start or (end - start).days > 366:
		raise_ione_error("CONFIGURATION_INVALID")
	rows = [
		{
			"period_index": index + 1,
			"period_start": max(getdate(first), start).isoformat(),
			"period_end": min(getdate(last), end).isoformat(),
		}
		for index, (first, last) in enumerate(budget.get_budget_periods())
	]
	if (
		not rows
		or len(rows) > 366
		or rows[0]["period_start"] != start.isoformat()
		or rows[-1]["period_end"] != end.isoformat()
	):
		raise_ione_error("CONFIGURATION_INVALID")
	for previous, current in pairwise(rows):
		if (
			date.fromisoformat(current["period_start"]) - date.fromisoformat(previous["period_end"])
		).days != 1:
			raise_ione_error("CONFIGURATION_INVALID")
	return rows


def version_inputs(
	company_name: str,
	hospital_name: str,
	organization_version: str,
	fiscal_year: str,
	*,
	lock=False,
	operational=True,
):
	company = native_document("Company", company_name, {"default_currency"}, lock=lock)
	hospital = native_document(
		"HRP Hospital", hospital_name, {"company", "enabled", "valid_from", "valid_to"}, lock=lock
	)
	org = native_document(
		"HRP Organization Version",
		organization_version,
		{"company", "hospital", "status", "effective_from"},
		lock=lock,
	)
	year = native_document(
		"Fiscal Year",
		fiscal_year,
		{"year_start_date", "year_end_date", "disabled", "companies.company"},
		lock=lock,
	)
	if hospital.company != company.name or org.company != company.name or org.hospital != hospital.name:
		raise_ione_error("CONFLICT")
	if operational and (
		org.docstatus != 1 or org.status != "Published" or year.disabled or not hospital.enabled
	):
		raise_ione_error("INVALID_STATE_TRANSITION")
	start, end = str(year.year_start_date), str(year.year_end_date)
	if org.effective_from and str(org.effective_from) > end:
		raise_ione_error("CONFLICT")
	if year.companies and company.name not in {row.company for row in year.companies}:
		raise_ione_error("CONFLICT")
	if not company.default_currency:
		raise_ione_error("CONFIGURATION_INVALID")
	native_document("Currency", company.default_currency, {"name"}, lock=lock)
	precision = get_field_precision(
		frappe.get_meta("Budget").get_field("budget_amount"), currency=company.default_currency
	)
	if not 0 <= precision <= 9:
		raise_ione_error("CONFIGURATION_INVALID")
	return {
		"company": company.name,
		"hospital": hospital.name,
		"organization_version": org.name,
		"fiscal_year": year.name,
		"currency": company.default_currency,
		"currency_precision": precision,
		"period_start": start,
		"period_end": end,
	}


def validate_version_inputs(version, *, lock=False):
	values = version_inputs(
		version.company, version.hospital, version.organization_version, version.fiscal_year, lock=lock
	)
	if any(str(version.get(key)) != str(value) for key, value in values.items()):
		raise_ione_error("CONFLICT")
	return values


def lock_hospital(name: str):
	return native_document("HRP Hospital", name, {"company", "enabled", "valid_from", "valid_to"}, lock=True)


def next_version_number(hospital: str, fiscal_year: str) -> int:
	# The hospital fence is held by the caller. A locking read sees versions
	# committed after the request's initial MVCC snapshot.
	rows = frappe.db.sql(
		"SELECT version_number FROM `tabHRP Budget Version` WHERE hospital=%s AND fiscal_year=%s ORDER BY version_number DESC LIMIT 1 FOR UPDATE",
		(hospital, fiscal_year),
	)
	return int(rows[0][0]) + 1 if rows else 1


def bare_result(doc):
	return {
		"doctype": doc.doctype,
		"name": doc.name,
		"revision": int(doc.revision),
		"docstatus": int(doc.docstatus),
	}


def execution_payload(execution):
	return {
		**execution.result,
		"idempotency_replayed": execution.idempotency_replayed,
		"correlation_id": execution.correlation_id,
		"request_id": execution.request_id,
	}


class SaveVersion(DomainService[VersionSave]):
	definition = DomainServiceDefinition(
		name="hrp_budget.version.save", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def request_payload(self, command):
		return command.as_request_payload()

	def validate(self, command):
		require_scope(
			VERSION,
			company=command.company,
			hospital=command.hospital,
			organization_unit=None,
			action="write" if command.name else "create",
		)
		version_inputs(
			command.company,
			command.hospital,
			command.organization_version,
			command.fiscal_year,
			operational=False,
		)
		if command.name:
			get_version(command.name)

	def authorize_result(self, result):
		get_version(str(result["name"]))

	def perform(self, command):
		lock_hospital(command.hospital)
		values = version_inputs(
			command.company, command.hospital, command.organization_version, command.fiscal_year, lock=True
		)
		with planning_write("save_version"):
			if command.name:
				doc = get_version(command.name, lock=True)
				if doc.docstatus != 0 or int(doc.revision) != command.expected_revision:
					raise_ione_error("CONFLICT")
				if (
					any(str(doc.get(key)) != str(value) for key, value in values.items())
					or doc.distribution_frequency != command.distribution_frequency
				):
					raise_ione_error("OPERATION_NOT_ALLOWED")
				doc.flags.locked_revision = int(doc.revision)
				doc.update({"subject": command.subject, "remarks": command.remarks})
				doc.save(ignore_permissions=True)
			else:
				doc = frappe.get_doc(
					{
						"doctype": VERSION,
						**values,
						"version_number": next_version_number(values["hospital"], values["fiscal_year"]),
						"subject": command.subject,
						"remarks": command.remarks,
						"distribution_frequency": command.distribution_frequency,
						"status": "Draft",
						"revision": 1,
					}
				)
				periods_for(doc)
				doc.insert(ignore_permissions=True)
		emit_audit_event("budget_version_saved", logger_name="medops.budget", revision=int(doc.revision))
		return bare_result(doc)


def save_budget_version(command: VersionSave, *, idempotency_key=None, correlation_id=None):
	return execution_payload(
		SaveVersion().execute(command, idempotency_key=idempotency_key, correlation_id=correlation_id)
	)


def canonical_lines(lines: tuple[PlanLine, ...], version, *, lock=False, operational=True):
	periods = periods_for(version)
	cache: dict[Any, Any] = {}
	result = []
	seen = set()
	for line in lines:
		if line.period_index > len(periods):
			raise_ione_error("INVALID_REQUEST")
		period = periods[line.period_index - 1]
		value = native_document(
			VALUE,
			line.budget_subject,
			{"company", "hospital", "dimension", "enabled", "valid_from", "valid_to"},
			lock=lock,
			cache=cache,
		)
		dimension = native_document(
			DIMENSION,
			value.dimension,
			{"company", "hospital", "purpose", "enabled", "valid_from", "valid_to"},
			lock=lock,
			cache=cache,
		)
		fund = native_document(
			FUND,
			line.funding_source,
			{"company", "hospital", "enabled", "valid_from", "valid_to"},
			lock=lock,
			cache=cache,
		)
		if operational and dimension.purpose != "Budget Subject":
			raise_ione_error("INVALID_REQUEST")
		for source in (value, dimension, fund):
			if source.company != version.company or source.hospital != version.hospital:
				raise_ione_error("CONFLICT")
			if operational:
				require_effective(source, period["period_start"], period["period_end"])
		account = native_document(
			"Account",
			line.account,
			{"company", "is_group", "report_type", "disabled"},
			lock=lock,
			cache=cache,
		)
		if operational and (
			account.company != version.company
			or account.is_group
			or account.report_type != "Profit and Loss"
			or account.disabled
		):
			raise_ione_error("INVALID_REQUEST")
		# Reuse the native accounting rule, without saving or submitting an ERP Budget.
		if operational:
			frappe.get_doc(
				{"doctype": "Budget", "company": version.company, "account": account.name}
			).validate_account()
		project = None
		if line.project:
			project = native_document("Project", line.project, {"company", "status"}, lock=lock, cache=cache)
			if operational and (
				project.company != version.company or project.status in {"Cancelled", "Completed"}
			):
				raise_ione_error("INVALID_STATE_TRANSITION")
		row = replace(
			line,
			budget_subject=value.name,
			funding_source=fund.name,
			account=account.name,
			project=project.name if project else None,
			amount=decimal_amount(line.amount, int(version.currency_precision)),
		)
		identity = line_identity(row)
		if identity in seen:
			raise_ione_error("INVALID_REQUEST")
		seen.add(identity)
		result.append(row)
	return tuple(sorted(result, key=line_sort_key))


def unit_context(version, unit_name: str, *, lock=False):
	unit = native_document(
		"HRP Organization Unit",
		unit_name,
		{"company", "hospital", "organization_version", "enabled", "valid_from", "valid_to"},
		lock=lock,
	)
	if (
		unit.company != version.company
		or unit.hospital != version.hospital
		or unit.organization_version != version.organization_version
		or not unit.enabled
	):
		raise_ione_error("CONFLICT")
	name = frappe.db.get_value(
		"HRP Organization Mapping", {"organization_unit": unit.name}, "name", for_update=lock
	)
	if not name:
		raise_ione_error("RESOURCE_NOT_FOUND")
	mapping = native_document(
		"HRP Organization Mapping",
		name,
		{"company", "hospital", "organization_unit", "organization_version", "cost_center", "enabled"},
		lock=lock,
	)
	if (
		not mapping.enabled
		or not mapping.cost_center
		or mapping.organization_version != version.organization_version
	):
		raise_ione_error("INVALID_STATE_TRANSITION")
	center = native_document(
		"Cost Center", mapping.cost_center, {"company", "is_group", "disabled"}, lock=lock
	)
	if center.company != version.company or center.disabled or center.is_group:
		raise_ione_error("INVALID_REQUEST")
	return unit, center


def plan_values(command: PlanSave, version, unit, center, *, lock=False):
	lines = canonical_lines(command.lines, version, lock=lock)
	periods = periods_for(version)
	for index in sorted({row.period_index for row in lines}):
		for effective in (periods[index - 1]["period_start"], periods[index - 1]["period_end"]):
			assert_master_data_hospital_scope(
				company=version.company,
				hospital=version.hospital,
				organization_unit=unit.name,
				effective_on=effective,
			)
	return {
		"company": version.company,
		"hospital": version.hospital,
		"budget_version": version.name,
		"organization_version": version.organization_version,
		"organization_unit": unit.name,
		"cost_center": center.name,
		"fiscal_year": version.fiscal_year,
		"currency": version.currency,
		"currency_precision": version.currency_precision,
		"subject": command.subject,
		"remarks": command.remarks,
		"lines": [
			{
				"line_key": line_identity(row),
				**row.as_request_payload(),
				**{key: periods[row.period_index - 1][key] for key in ("period_start", "period_end")},
			}
			for row in lines
		],
	}


class SavePlan(DomainService[PlanSave]):
	definition = DomainServiceDefinition(
		name="hrp_budget.plan.save", version=1, kind="command", required_roles=WRITE_ROLES
	)

	def request_payload(self, command):
		return command.as_request_payload()

	def validate(self, command):
		version = get_version(command.budget_version)
		require_scope(
			PLAN,
			company=version.company,
			hospital=version.hospital,
			organization_unit=command.organization_unit,
			action="write" if command.name else "create",
		)
		if command.name:
			get_plan(command.name)

	def authorize_result(self, result):
		get_plan(str(result["name"]))

	def perform(self, command):
		preliminary = get_version(command.budget_version)
		lock_hospital(preliminary.hospital)
		version = get_version(command.budget_version, lock=True)
		if version.docstatus or version.status != "Draft":
			raise_ione_error("INVALID_STATE_TRANSITION")
		validate_version_inputs(version, lock=True)
		unit, center = unit_context(version, command.organization_unit, lock=True)
		values = plan_values(command, version, unit, center, lock=True)
		identity = fingerprint_json({"budget_version": version.name, "organization_unit": unit.name})
		exists = frappe.db.get_value(PLAN, identity, "name", for_update=True)
		if bool(exists) != bool(command.name) or (command.name and command.name != exists):
			raise_ione_error("CONFLICT")
		with planning_write("save_plan"):
			if exists:
				doc = get_plan(exists, lock=True)
				if int(doc.revision) != command.expected_revision or doc.docstatus:
					raise_ione_error("CONFLICT")
				doc.flags.locked_revision = int(doc.revision)
				doc.update({**values, "status": "Draft"})
				doc.save(ignore_permissions=True)
			else:
				doc = frappe.get_doc(
					{
						"doctype": PLAN,
						**values,
						"identity_key": identity,
						"applicant": frappe.session.user,
						"posting_date": today(),
						"status": "Draft",
						"revision": 1,
					}
				).insert(ignore_permissions=True)
		enforce_business_rules(doc, "validate", required=False)
		emit_audit_event(
			"budget_plan_saved",
			logger_name="medops.budget",
			revision=int(doc.revision),
			line_count=len(doc.lines),
		)
		return bare_result(doc)


def save_budget_plan(command: PlanSave, *, idempotency_key=None, correlation_id=None):
	require_authenticated_user()
	require_roles(WRITE_ROLES)
	version = get_version(command.budget_version)
	require_scope(
		PLAN,
		company=version.company,
		hospital=version.hospital,
		organization_unit=command.organization_unit,
		action="write" if command.name else "create",
	)
	# A successful retry needs current read permission, not today's operational
	# eligibility. New writes revalidate live mappings and sources in perform().
	unit = native_document(
		"HRP Organization Unit",
		command.organization_unit,
		{"company", "hospital", "organization_version", "enabled", "valid_from", "valid_to"},
	)
	canonical = replace(
		command,
		budget_version=version.name,
		organization_unit=unit.name,
		lines=canonical_lines(command.lines, version, operational=False),
	)
	return execution_payload(
		SavePlan().execute(canonical, idempotency_key=idempotency_key, correlation_id=correlation_id)
	)


def locked_plan_names(version_name: str) -> list[str]:
	rows = frappe.db.sql(
		"SELECT name FROM `tabHRP Budget Plan` WHERE budget_version=%s ORDER BY name LIMIT %s FOR UPDATE",
		(version_name, MAX_PLANS + 1),
	)
	if len(rows) > MAX_PLANS:
		raise_ione_error("INVALID_REQUEST")
	return [row[0] for row in rows]


class FreezeVersion(DomainService[PlanningAction]):
	definition = DomainServiceDefinition(
		name="hrp_budget.version.freeze", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def request_payload(self, command):
		return command.as_request_payload()

	def validate(self, command):
		version = get_version(command.name)
		require_planning_scope(version, VERSION, "submit")

	def authorize_result(self, result):
		get_version(str(result["name"]))

	def perform(self, command):
		preliminary = get_version(command.name)
		lock_hospital(preliminary.hospital)
		version = get_version(command.name, lock=True)
		if version.docstatus or int(version.revision) != command.expected_revision:
			raise_ione_error("CONFLICT")
		validate_version_inputs(version, lock=True)
		refs = []
		active = 0
		row_count = 0
		for name in locked_plan_names(version.name):
			doc = get_plan(name, lock=True)
			row_count += len(doc.lines)
			if row_count > MAX_LINES * 10:
				raise_ione_error("INVALID_REQUEST")
			if doc.status == "Draft":
				active += 1
				unit, center = unit_context(version, doc.organization_unit, lock=True)
				if center.name != doc.cost_center:
					raise_ione_error("CONFLICT")
				from medops.common.budget_planning import build_plan

				plan_values(
					build_plan(
						{
							"name": doc.name,
							"expected_revision": int(doc.revision),
							"budget_version": version.name,
							"organization_unit": unit.name,
							"subject": doc.subject,
							"remarks": doc.remarks,
							"lines": [
								{
									key: row.get(key)
									for key in (
										"budget_subject",
										"funding_source",
										"account",
										"project",
										"period_index",
										"amount",
										"description",
									)
								}
								for row in doc.lines
							],
						}
					),
					version,
					unit,
					center,
					lock=True,
				)
				enforce_business_rules(doc, "validate", required=False)
			refs.append(
				{
					"name": doc.name,
					"included": doc.status != "Withdrawn",
					"digest": planning_digest(plan_definition(doc.as_public_dict())),
				}
			)
		if not active:
			raise_ione_error("INVALID_STATE_TRANSITION")
		with planning_write("freeze"):
			version.flags.locked_revision = int(version.revision)
			version.snapshot_digest = planning_digest(
				{"version": version_definition(version.as_public_dict()), "plans": refs}
			)
			version.status = "Frozen"
			version.frozen_at = now_datetime()
			version.frozen_by = frappe.session.user
			version.flags.ignore_permissions = True
			version.submit()
		emit_audit_event(
			"budget_version_frozen",
			logger_name="medops.budget",
			revision=int(version.revision),
			plan_count=len(refs),
			line_count=row_count,
		)
		return bare_result(version)


def freeze_budget_version(command: PlanningAction, *, idempotency_key=None, correlation_id=None):
	return execution_payload(
		FreezeVersion().execute(command, idempotency_key=idempotency_key, correlation_id=correlation_id)
	)


def snapshot_of_version(version):
	refs = []
	names = locked_plan_names(version.name)
	row_count = 0
	for name in names:
		doc = get_plan(name, lock=True)
		row_count += len(doc.lines)
		if row_count > MAX_LINES * 10:
			raise_ione_error("INVALID_REQUEST")
		refs.append(
			{
				"name": doc.name,
				"included": doc.status != "Withdrawn",
				"digest": planning_digest(plan_definition(doc.as_public_dict())),
			}
		)
	return planning_digest({"version": version_definition(version.as_public_dict()), "plans": refs}), names


class CloneVersion(DomainService[PlanningAction]):
	definition = DomainServiceDefinition(
		name="hrp_budget.version.clone", version=1, kind="command", required_roles=MANAGER_ROLES
	)

	def request_payload(self, command):
		return command.as_request_payload()

	def validate(self, command):
		version = get_version(command.name)
		require_planning_scope(version, VERSION, "create")

	def authorize_result(self, result):
		get_version(str(result["name"]))

	def perform(self, command):
		preliminary = get_version(command.name)
		lock_hospital(preliminary.hospital)
		source = get_version(command.name, lock=True)
		if (
			source.docstatus != 1
			or source.status != "Frozen"
			or int(source.revision) != command.expected_revision
		):
			raise_ione_error("CONFLICT")
		values = validate_version_inputs(source, lock=True)
		digest, names = snapshot_of_version(source)
		if digest != source.snapshot_digest:
			raise_ione_error("CONFIGURATION_INVALID")
		with planning_write("clone"):
			version = frappe.get_doc(
				{
					"doctype": VERSION,
					**values,
					"version_number": next_version_number(source.hospital, source.fiscal_year),
					"subject": command.subject or source.subject,
					"distribution_frequency": source.distribution_frequency,
					"remarks": source.remarks,
					"cloned_from": source.name,
					"status": "Draft",
					"revision": 1,
				}
			).insert(ignore_permissions=True)
			copied = 0
			for name in names:
				old = get_plan(name, lock=True)
				if old.status == "Withdrawn":
					continue
				require_scope(
					PLAN,
					company=version.company,
					hospital=version.hospital,
					organization_unit=old.organization_unit,
					action="create",
				)
				unit, center = unit_context(version, old.organization_unit, lock=True)
				if center.name != old.cost_center:
					raise_ione_error("CONFLICT")
				from medops.common.budget_planning import build_plan

				command_plan = build_plan(
					{
						"budget_version": version.name,
						"organization_unit": unit.name,
						"subject": old.subject,
						"remarks": old.remarks,
						"lines": [
							{
								key: row.get(key)
								for key in (
									"budget_subject",
									"funding_source",
									"account",
									"project",
									"period_index",
									"amount",
									"description",
								)
							}
							for row in old.lines
						],
					}
				)
				body = plan_values(command_plan, version, unit, center, lock=True)
				identity = fingerprint_json({"budget_version": version.name, "organization_unit": unit.name})
				new = frappe.get_doc(
					{
						"doctype": PLAN,
						**body,
						"identity_key": identity,
						"source_plan": old.name,
						"applicant": frappe.session.user,
						"posting_date": today(),
						"status": "Withdrawn" if old.status == "Withdrawn" else "Draft",
						"revision": 1,
					}
				).insert(ignore_permissions=True)
				get_plan(new.name)
				enforce_business_rules(new, "validate", required=False)
				copied += 1
		emit_audit_event("budget_version_cloned", logger_name="medops.budget", plan_count=copied)
		return bare_result(version)


def clone_budget_version(command: PlanningAction, *, idempotency_key=None, correlation_id=None):
	return execution_payload(
		CloneVersion().execute(command, idempotency_key=idempotency_key, correlation_id=correlation_id)
	)


class WithdrawPlan(DomainService[PlanningAction]):
	definition = DomainServiceDefinition(
		name="hrp_budget.plan.withdraw", version=1, kind="command", required_roles=WRITE_ROLES
	)

	def request_payload(self, command):
		return command.as_request_payload()

	def validate(self, command):
		doc = get_plan(command.name)
		require_planning_scope(doc, PLAN, "write")

	def authorize_result(self, result):
		get_plan(str(result["name"]))

	def perform(self, command):
		preliminary = get_plan(command.name)
		lock_hospital(preliminary.hospital)
		version = get_version(preliminary.budget_version, lock=True)
		doc = get_plan(command.name, lock=True)
		if (
			version.docstatus
			or doc.docstatus
			or doc.status != "Draft"
			or int(doc.revision) != command.expected_revision
		):
			raise_ione_error("INVALID_STATE_TRANSITION")
		with planning_write("withdraw"):
			doc.flags.locked_revision = int(doc.revision)
			doc.status = "Withdrawn"
			doc.save(ignore_permissions=True)
		return bare_result(doc)


def withdraw_budget_plan(command: PlanningAction, *, idempotency_key=None, correlation_id=None):
	return execution_payload(
		WithdrawPlan().execute(command, idempotency_key=idempotency_key, correlation_id=correlation_id)
	)


def read_budget(kind: str, name: str):
	with service_audit_scope():
		require_authenticated_user()
		require_roles(READ_ROLES)
		if kind == "version":
			doc = get_version(name)
			return {**doc.as_public_dict(), "periods": periods_for(doc)}
		if kind == "plan":
			return get_plan(name).as_public_dict()
		raise_ione_error("INVALID_REQUEST")


def list_budget(kind: str, filters: dict[str, Any], *, offset=0, limit=50, search=""):
	with service_audit_scope():
		require_authenticated_user()
		require_roles(READ_ROLES)
		if (
			kind not in {"version", "plan"}
			or not 0 <= offset <= 5000
			or not 1 <= limit <= 100
			or len(search) > 140
		):
			raise_ione_error("INVALID_REQUEST")
		allowed = {"name", "company", "hospital", "fiscal_year", "status"} | (
			{"budget_version", "organization_unit"} if kind == "plan" else set()
		)
		if set(filters) - allowed:
			raise_ione_error("INVALID_REQUEST")
		doctype = VERSION if kind == "version" else PLAN
		with planning_query_scope():
			names = frappe.get_list(
				doctype,
				filters=filters,
				pluck="name",
				order_by="modified desc,name desc",
				limit=5001,
				or_filters={field: ["like", "%" + search + "%"] for field in ("name", "subject")}
				if search
				else None,
			)
		if len(names) > 5000:
			raise_ione_error("INVALID_REQUEST")
		visible = []
		for name in names:
			doc = frappe.get_doc(doctype, name)
			if frappe.has_permission(doctype, "read", doc=doc) and (
				version_permission(doc) if kind == "version" else plan_permission(doc)
			):
				visible.append(
					{
						key: doc.get(key)
						for key in (
							"name",
							"subject",
							"company",
							"hospital",
							"fiscal_year",
							"status",
							"revision",
						)
					}
				)
		return {
			"items": visible[offset : offset + limit],
			"next_offset": offset + limit if len(visible) > offset + limit else None,
			"limit": limit,
		}


def spread_budget_amount(version_name: str, amount: object):
	require_authenticated_user()
	require_roles(READ_ROLES)
	version = get_version(version_name)
	periods = periods_for(version)
	amounts = allocate_amount(amount, len(periods), int(version.currency_precision))
	return [{**period, "amount": value} for period, value in zip(periods, amounts, strict=True)]
