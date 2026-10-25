from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from typing import Any, cast

import frappe
from frappe.desk.doctype.notification_log.notification_log import make_notification_logs
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime

from medops.common.domain_service import DomainServiceDefinition, canonical_json
from medops.common.error_catalog import IoneApplicationError
from medops.common.sla import SLAClock, SLAContractError, policy_payload, sla_digest
from medops.hrp_workflow_authorization.doctype.hrp_sla_policy.hrp_sla_policy import HRPSLAPolicy
from medops.hrp_workflow_authorization.permissions import has_scoped_permission
from medops.hrp_workflow_authorization.services.segregation import get_governed_document_context
from medops.services.audit_context import emit_audit_event
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_authenticated_user, require_roles

POLICY = "HRP SLA Policy"
CASE = "HRP SLA Escalation"
ADMIN_ROLES = frozenset({"System Manager", "HRP System Manager"})
TODO_PREFIX = "MEDOPS-SLA-"
MAX_POLICY_CANDIDATES = 200
SCHEDULER_BATCH_SIZE = 50


def _admin(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or bool(ADMIN_ROLES.intersection(frappe.get_roles(user)))


def _source(case: Document, *, lock: bool = False) -> Document:
	if not frappe.db.exists(case.reference_doctype, case.reference_name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	if lock:
		return frappe.get_doc(case.reference_doctype, case.reference_name, for_update=True)
	return frappe.get_doc(case.reference_doctype, case.reference_name)


def _readable(source: Document, user: str) -> bool:
	state = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
	return bool(
		state
		and state.enabled
		and state.user_type == "System User"
		and frappe.has_permission(source.doctype, ptype="read", doc=source, user=user)
		and has_scoped_permission(source, user=user, ptype="read")
	)


def _require_read(source: Document, user: str | None = None) -> None:
	if not _readable(source, user or frappe.session.user):
		raise_ione_error("PERMISSION_DENIED")


def _case(name: str, *, lock: bool = False) -> Document:
	if not frappe.db.exists(CASE, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	return frappe.get_doc(CASE, name, for_update=lock)


def _save(case: Document) -> None:
	case.flags.sla_service_write = True
	case.save(ignore_permissions=True)
	for todo_name, kind in case.flags.sla_new_notifications or []:
		frappe.enqueue(
			"medops.hrp_workflow_authorization.services.sla.notify_current_task",
			case_name=case.name,
			todo_name=todo_name,
			kind=kind,
			enqueue_after_commit=not frappe.in_test,
			now=frappe.in_test,
		)
	case.flags.sla_new_notifications = []
	# No source identifiers, user addresses or free-text reasons in audit logs.
	emit_audit_event(
		"sla_cycle_saved",
		logger_name="medops.sla",
		status=case.status,
		revision=int(case.revision),
		policy_digest=case.policy_digest,
		response_level=int(case.response_level or 0),
		resolution_level=int(case.resolution_level or 0),
	)


def freeze_calendar(policy: dict[str, Any]) -> dict[str, Any]:
	from erpnext.support.doctype.service_level_agreement.service_level_agreement import (
		get_support_days,
		get_time_in_timedelta,
	)

	rows = [
		frappe._dict(
			workday=row["workday"],
			start_time=get_time_in_timedelta(time.fromisoformat(row["start_time"])),
			end_time=get_time_in_timedelta(time.fromisoformat(row["end_time"])),
		)
		for row in json.loads(policy["support_hours_json"])
	]
	days = get_support_days(frappe._dict(support_and_resolution=rows))
	calendar: dict[str, Any] = {
		"basis": policy["clock_basis"],
		"days": {
			key: [value.start_time.total_seconds(), value.end_time.total_seconds()]
			for key, value in days.items()
		},
		"holidays": [],
		"valid_from": None,
		"valid_to": None,
	}
	if policy["holiday_list"]:
		doc = frappe.get_doc("Holiday List", policy["holiday_list"])
		if len(doc.holidays) > 3660:
			raise_ione_error("CONFIGURATION_INVALID")
		calendar.update(
			holidays=sorted({str(row.holiday_date) for row in doc.holidays}),
			valid_from=str(doc.from_date),
			valid_to=str(doc.to_date),
		)
	return calendar


def _snapshot(case: Document) -> tuple[dict[str, Any], SLAClock]:
	try:
		snapshot = json.loads(case.snapshot_json)
		if (
			sla_digest(snapshot) != case.snapshot_digest
			or sla_digest(snapshot["policy"]) != case.policy_digest
		):
			raise ValueError("snapshot digest mismatch")
		calendar = snapshot["calendar"]
		clock = SLAClock(
			calendar["basis"],
			{key: tuple(value) for key, value in calendar["days"].items()},
			frozenset(calendar["holidays"]),
			calendar["valid_from"],
			calendar["valid_to"],
		)
		return snapshot["policy"], clock
	except (KeyError, TypeError, ValueError) as exc:
		raise_ione_error("CONFIGURATION_INVALID", cause=exc)


def _close_tasks(case: Document, *, kind: str | None = None, status: str = "Cancelled") -> None:
	assignments = json.loads(case.current_assignments_json or "{}")
	for key, name in list(assignments.items()):
		if kind and kind != key:
			continue
		if frappe.db.exists("ToDo", name):
			todo = frappe.get_doc("ToDo", name)
			if (
				not name.startswith(TODO_PREFIX)
				or todo.reference_type != case.reference_doctype
				or todo.reference_name != case.reference_name
			):
				raise_ione_error("CONFIGURATION_INVALID")
			if todo.status == "Open":
				todo.flags.sla_service_write = True
				todo.status = status
				todo.save(ignore_permissions=True)
		assignments.pop(key)
	case.current_assignments_json = canonical_json(assignments)


def _finish(case: Document, status: str, at: datetime) -> None:
	_close_tasks(case, status="Closed" if status == "Completed" else "Cancelled")
	case.status = status
	case.completed_at = at
	case.active_key = None
	case.active_since = None
	case.next_run_at = None
	case.last_error_code = None
	case.retry_count = 0
	_save(case)


def _clock_total(case: Document, clock: SLAClock, at: datetime) -> float:
	return float(case.elapsed_work_seconds or 0) + (
		clock.seconds(get_datetime(case.active_since), at) if case.active_since else 0.0
	)


def _next_deadline(
	case: Document, policy: dict[str, Any], clock: SLAClock, total: float, at: datetime
) -> datetime | None:
	remaining = []
	for kind in ("response", "resolution"):
		if kind == "response" and case.responded_at:
			continue
		limit = policy[kind + "_seconds"]
		if total < limit:
			remaining.append(limit - total)
		for step in json.loads(policy["escalation_steps_json"]):
			threshold = limit + step["delay_seconds"]
			if step["level"] > int(case.get(kind + "_level") or 0) and threshold > total:
				remaining.append(threshold - total)
	return clock.due(at, min(remaining)) if remaining else None


def _task(case: Document, source: Document, kind: str, step: dict[str, Any], at: datetime) -> None:
	user = step["assigned_to"]
	_require_read(source, user)
	level = int(step["level"])
	name = TODO_PREFIX + sla_digest([case.cycle_key, kind, level])[:40]
	_close_tasks(case, kind=kind)
	if frappe.db.exists("ToDo", name):
		# A completed task stays completed; replay must never recreate or reopen it.
		todo = frappe.get_doc("ToDo", name)
		if (
			todo.allocated_to != user
			or todo.reference_type != source.doctype
			or todo.reference_name != source.name
		):
			raise_ione_error("CONFIGURATION_INVALID")
	else:
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"allocated_to": user,
				"assigned_by": frappe.session.user,
				"reference_type": source.doctype,
				"reference_name": source.name,
				"status": "Open",
				"priority": "High",
				"date": at.date(),
				"description": f"服务时限超时：{'响应' if kind == 'response' else '处理'}；升级层级 {level}。请处理业务单据。",
			}
		)
		todo.flags.sla_service_write = True
		todo.insert(ignore_permissions=True, set_name=name)
		pending = list(case.flags.sla_new_notifications or [])
		pending.append((name, kind))
		case.flags.sla_new_notifications = pending
	assignments = json.loads(case.current_assignments_json or "{}")
	assignments[kind] = name
	case.current_assignments_json = canonical_json(assignments)
	case.set(kind + "_level", level)


def notify_current_task(case_name: str, todo_name: str, kind: str) -> bool:
	"""A savepoint rollback does not remove Frappe after-commit callbacks: recheck durable state."""
	if kind not in {"response", "resolution"} or not todo_name.startswith(TODO_PREFIX):
		return False
	if not frappe.db.exists(CASE, case_name) or not frappe.db.exists("ToDo", todo_name):
		return False
	# Serialize notification deduplication; this job does not lock or modify the business source.
	todo = frappe.get_doc("ToDo", todo_name, for_update=True)
	case = _case(case_name)
	if (
		case.status not in {"Open", "Paused"}
		or todo.status != "Open"
		or json.loads(case.current_assignments_json or "{}").get(kind) != todo_name
	):
		return False
	if todo.reference_type != case.reference_doctype or todo.reference_name != case.reference_name:
		return False
	if not frappe.db.exists(case.reference_doctype, case.reference_name):
		return False
	if not _readable(_source(case), todo.allocated_to):
		return False
	email = frappe.db.get_value("User", todo.allocated_to, "email")
	if not email:
		return False
	make_notification_logs(
		frappe._dict(
			{
				"type": "Alert",
				"subject": f"服务时限超时：第 {int(case.get(kind + '_level') or 0)} 级",
				"document_type": case.reference_doctype,
				"document_name": case.reference_name,
				"from_user": todo.assigned_by,
				"email_content": "请打开业务单据查看待处理事项。",
				"link": f"/desk/todo/{todo_name}",
				"dedupe_on": ["link"],
			}
		),
		[email],
	)
	return True


def _advance(case: Document, source: Document, at: datetime) -> None:
	if case.status in {"Completed", "Cancelled"}:
		return
	# Terminal cleanup must remain possible even with a stale/corrupt monitoring calendar.
	if source.docstatus == 2:
		_finish(case, "Cancelled", at)
		return
	policy, clock = _snapshot(case)
	status = str(source.get(policy["status_field"]) or "")
	if status in json.loads(policy["completion_statuses_json"]):
		try:
			if policy["response_field"] and source.get(policy["response_field"]) and not case.responded_at:
				responded = get_datetime(source.get(policy["response_field"]))
				if not get_datetime(case.last_processed_at or case.started_at) <= responded <= at:
					raise SLAContractError("response timestamp cannot be reconstructed")
				response_total = _clock_total(case, clock, responded)
				case.responded_at = responded
				case.response_breached = int(
					bool(case.response_breached) or response_total >= policy["response_seconds"]
				)
			total = _clock_total(case, clock, at)
			case.elapsed_work_seconds = total
			for kind in ("response", "resolution"):
				if (kind != "response" or not case.responded_at) and total >= policy[kind + "_seconds"]:
					case.set(kind + "_breached", 1)
			unavailable = False
		except SLAContractError:
			# Calendar expiry must not prevent a hospital from completing business.
			unavailable = True
		_finish(case, "Completed", at)
		if unavailable:
			case.last_error_code = "SLA-CLOCK-UNAVAILABLE"
			case.last_error_at = at
			_save(case)
		return
	if policy["response_field"] and source.get(policy["response_field"]) and not case.responded_at:
		responded = get_datetime(source.get(policy["response_field"]))
		lower = get_datetime(case.last_processed_at or case.started_at)
		if responded < lower or responded > at:
			raise_ione_error("CONFIGURATION_INVALID")
		response_total = _clock_total(case, clock, responded)
		case.responded_at = responded
		case.response_breached = int(
			bool(case.response_breached) or response_total >= policy["response_seconds"]
		)
		_close_tasks(case, kind="response", status="Closed")
	paused = status in json.loads(policy["pause_statuses_json"])
	if paused:
		if case.status != "Paused":
			case.elapsed_work_seconds = _clock_total(case, clock, at)
			for kind in ("response", "resolution"):
				if (kind != "response" or not case.responded_at) and case.elapsed_work_seconds >= policy[
					kind + "_seconds"
				]:
					case.set(kind + "_breached", 1)
			case.active_since = None
			case.paused_at = at
			case.status = "Paused"
		# The source hook resumes the clock; paused cases consume no scheduler slots.
		case.next_run_at = None
		case.last_processed_at = at
		case.last_error_code = None
		case.retry_count = 0
		_save(case)
		return
	if case.status == "Paused":
		case.status = "Open"
		case.active_since = at
		case.paused_at = None
	total = _clock_total(case, clock, at)
	for kind in ("response", "resolution"):
		if kind == "response" and case.responded_at:
			continue
		limit = policy[kind + "_seconds"]
		remaining = max(0.0, limit - total)
		if remaining > 0:
			case.set(kind + "_due_at", clock.due(at, remaining))
		if total >= limit:
			case.set(kind + "_breached", 1)
			due = [
				step
				for step in json.loads(policy["escalation_steps_json"])
				if total >= limit + step["delay_seconds"]
			]
			if due and due[-1]["level"] > int(case.get(kind + "_level") or 0):
				_task(case, source, kind, due[-1], at)
	case.last_processed_at = at
	case.next_run_at = _next_deadline(case, policy, clock, total, at)
	case.last_error_code = None
	case.retry_count = 0
	_save(case)


def _choose_policy(source: Document) -> HRPSLAPolicy | None:
	context = get_governed_document_context(source)
	ancestors: list[str] = []
	unit = context.organization_unit
	for _ in range(2000):
		if not unit:
			break
		if unit in ancestors:
			raise_ione_error("CONFIGURATION_INVALID")
		ancestors.append(unit)
		unit = frappe.db.get_value("HRP Organization Unit", unit, "parent_organization_unit")
	else:
		raise_ione_error("CONFIGURATION_INVALID")
	rows = frappe.db.sql(
		"""SELECT name, organization_unit, include_descendants, priority
		FROM `tabHRP SLA Policy` WHERE target_doctype=%(doctype)s AND company=%(company)s
		AND hospital=%(hospital)s AND enabled=1 AND valid_from<=%(date)s
		AND (valid_to IS NULL OR valid_to>=%(date)s)
		AND (organization_unit IS NULL OR organization_unit='' OR organization_unit IN %(units)s)
		ORDER BY name LIMIT 201""",
		{
			"doctype": source.doctype,
			"company": context.company,
			"hospital": context.hospital,
			"date": context.effective_on,
			"units": tuple(ancestors),
		},
		as_dict=True,
	)
	if len(rows) > MAX_POLICY_CANDIDATES:
		raise_ione_error("CONFIGURATION_INVALID")
	ranked = []
	for row in rows:
		if (
			row.organization_unit
			and row.organization_unit != context.organization_unit
			and not row.include_descendants
		):
			continue
		distance = ancestors.index(row.organization_unit) if row.organization_unit else 2001
		ranked.append(((distance, -int(row.priority)), row.name))
	if not ranked:
		return None
	ranked.sort()
	if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
		raise_ione_error("CONFIGURATION_INVALID")
	return cast(HRPSLAPolicy, frappe.get_doc(POLICY, ranked[0][1]))


def sync_source(source: Document, event: str | None = None) -> None:
	"""Observe the source in its existing transaction; never change its workflow."""
	if (
		frappe.flags.in_install
		or frappe.flags.in_migrate
		or frappe.flags.in_patch
		or source.doctype in {POLICY, CASE, "ToDo", "Notification Log"}
	):
		return
	if not frappe.db.table_exists(POLICY):
		return
	# Registered target list is request-local and invalidated when policies change.
	targets = getattr(frappe.local, "medops_sla_targets", None)
	if targets is None:
		targets = set(frappe.get_all(POLICY, pluck="target_doctype", distinct=True, limit=400))
		frappe.local.medops_sla_targets = targets
	if source.doctype not in targets:
		return
	active_key = sla_digest([source.doctype, source.name])
	active_name = frappe.db.get_value(CASE, {"active_key": active_key}, "name")
	at = now_datetime()
	if active_name:
		case = _case(active_name, lock=True)
		if event == "on_trash":
			_finish(case, "Cancelled", at)
			return
		policy, _ = _snapshot(case)
		anchor = source.get(policy["start_field"])
		if anchor and get_datetime(anchor) != get_datetime(case.source_start_at):
			_finish(case, "Cancelled", at)
		else:
			_advance(case, source, at)
			return
	if source.docstatus == 2 or event == "on_trash":
		return
	selected = _choose_policy(source)
	if selected is None:
		return
	selected.validate_configuration()
	policy = selected.as_policy()
	if source.get(policy["status_field"]) not in json.loads(policy["start_statuses_json"]):
		return
	anchor = source.get(policy["start_field"])
	if not anchor:
		raise_ione_error("CONFIGURATION_INVALID")
	started = get_datetime(anchor)
	if started > at:
		raise_ione_error("CONFIGURATION_INVALID")
	key = sla_digest([source.doctype, source.name, str(started)])
	if frappe.db.exists(CASE, {"cycle_key": key}):
		return
	context = get_governed_document_context(source)
	_require_read(source, policy["responsible_user"])
	snapshot = {"schema_version": 1, "policy": policy, "calendar": freeze_calendar(policy)}
	case = frappe.get_doc(
		{
			"doctype": CASE,
			"cycle_key": key,
			"active_key": active_key,
			"reference_doctype": source.doctype,
			"reference_name": source.name,
			"source_start_at": started,
			"sla_policy": selected.name,
			"policy_revision": selected.revision,
			"policy_digest": selected.policy_digest,
			"snapshot_json": canonical_json(snapshot),
			"snapshot_digest": sla_digest(snapshot),
			"company": context.company,
			"hospital": context.hospital,
			"organization_unit": context.organization_unit,
			"status": "Open",
			"started_at": started,
			"active_since": started,
			"responsible_user": policy["responsible_user"],
			"current_assignments_json": "{}",
			"next_run_at": at,
			"revision": 1,
		}
	)
	case.flags.sla_service_write = True
	case.insert(ignore_permissions=True)
	_, clock = _snapshot(case)
	case.response_due_at = clock.due(started, policy["response_seconds"])
	case.resolution_due_at = clock.due(started, policy["resolution_seconds"])
	_advance(case, source, at)


def protect_todo(doc: Document, event: str | None = None) -> None:
	before = doc.get_doc_before_save()
	owned = str(doc.name or "").startswith(TODO_PREFIX) or bool(
		before and str(before.name).startswith(TODO_PREFIX)
	)
	if not owned or doc.flags.sla_service_write:
		return
	# Standard recipient closure is allowed; changing recipient/reference or deleting is not.
	if event in {"on_trash", "before_rename"} or not before:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	if (
		any(
			before.get(key) != doc.get(key)
			for key in [
				"name",
				*(field.fieldname for field in doc.meta.fields if field.fieldname != "status"),
			]
		)
		or before.status != "Open"
		or doc.status != "Closed"
		or doc.allocated_to != frappe.session.user
	):
		raise_ione_error("OPERATION_NOT_ALLOWED")
	_require_read(frappe.get_doc(doc.reference_type, doc.reference_name))


class UpsertPolicy(DomainService[dict[str, Any]]):
	definition = DomainServiceDefinition(
		name="hrp_workflow_authorization.sla_policy.upsert",
		version=1,
		kind="command",
		required_roles=ADMIN_ROLES,
	)

	def request_payload(self, command: dict[str, Any]) -> dict[str, object]:
		return command

	def perform(self, command: dict[str, Any]) -> dict[str, object]:
		payload = policy_payload(command["policy"])
		name = payload["code"]
		exists = frappe.db.exists(POLICY, name)
		if exists:
			doc = frappe.get_doc(POLICY, name, for_update=True)
			if int(doc.revision) != command["expected_revision"]:
				raise_ione_error("CONFLICT")
			doc.update(payload)
		else:
			if command["expected_revision"] != 0:
				raise_ione_error("CONFLICT")
			doc = frappe.get_doc({"doctype": POLICY, **payload})
		doc.flags.sla_service_write = True
		doc.save(ignore_permissions=True) if exists else doc.insert(ignore_permissions=True)
		frappe.local.medops_sla_targets = None
		emit_audit_event(
			"sla_policy_saved",
			logger_name="medops.sla",
			policy_digest=doc.policy_digest,
			revision=int(doc.revision),
			enabled=bool(doc.enabled),
		)
		return {
			"name": doc.name,
			"revision": int(doc.revision),
			"policy_digest": doc.policy_digest,
			"policy": payload,
		}


class CaseAction(DomainService[dict[str, Any]]):
	definition = DomainServiceDefinition(
		name="hrp_workflow_authorization.sla_case.action",
		version=1,
		kind="command",
		required_roles=frozenset({"Desk User"}),
	)

	def authorize(self, command: dict[str, Any]) -> None:
		require_authenticated_user()
		case = _case(command["case_name"])
		_require_read(_source(case))
		if command["action"] in {"process", "cancel"}:
			require_roles(ADMIN_ROLES)
		elif command["action"] == "acknowledge":
			users = {case.responsible_user}
			for name in json.loads(case.current_assignments_json or "{}").values():
				users.add(frappe.db.get_value("ToDo", name, "allocated_to"))
			if not _admin() and frappe.session.user not in users:
				raise_ione_error("PERMISSION_DENIED")
		else:
			raise_ione_error("INVALID_REQUEST")

	def request_payload(self, command: dict[str, Any]) -> dict[str, object]:
		return command

	def perform(self, command: dict[str, Any]) -> dict[str, object]:
		initial = _case(command["case_name"])
		source = _source(initial, lock=True)
		case = _case(initial.name, lock=True)
		if command["expected_revision"] is not None and int(case.revision) != command["expected_revision"]:
			raise_ione_error("CONFLICT")
		at = now_datetime()
		if command["action"] == "process":
			_advance(case, source, at)
		elif command["action"] == "cancel":
			if case.status in {"Completed", "Cancelled"}:
				raise_ione_error("INVALID_STATE_TRANSITION")
			case.disposition_reason = command["reason"]
			case.disposition_by = frappe.session.user
			_finish(case, "Cancelled", at)
		else:
			if case.status != "Open" or case.responded_at:
				raise_ione_error("INVALID_STATE_TRANSITION")
			policy, clock = _snapshot(case)
			case.response_breached = int(
				bool(case.response_breached) or _clock_total(case, clock, at) >= policy["response_seconds"]
			)
			case.responded_at = at
			_close_tasks(case, kind="response", status="Closed")
			_save(case)
		return public_case(case)


def public_case(case: Document) -> dict[str, object]:
	fields = (
		"name",
		"reference_doctype",
		"reference_name",
		"sla_policy",
		"policy_revision",
		"status",
		"started_at",
		"response_due_at",
		"resolution_due_at",
		"responded_at",
		"completed_at",
		"paused_at",
		"response_breached",
		"resolution_breached",
		"response_level",
		"resolution_level",
		"responsible_user",
		"next_run_at",
		"last_error_code",
		"retry_count",
		"revision",
	)
	return {
		key: str(case.get(key)) if isinstance(case.get(key), datetime) else case.get(key) for key in fields
	}


def get_case(name: str) -> dict[str, object]:
	case = _case(name)
	_require_read(_source(case))
	return public_case(case)


def list_cases(*, offset: int = 0, limit: int = 20) -> dict[str, object]:
	# Bound the database scan and return a cursor even when some rows fail source permission.
	from medops.hrp_workflow_authorization.permissions import scope_permission_query

	conditions = scope_permission_query(CASE)
	rows = frappe.db.sql(
		"SELECT name FROM `tabHRP SLA Escalation`"
		+ (" WHERE " + conditions if conditions else "")
		+ " ORDER BY modified DESC, name DESC LIMIT %s OFFSET %s",
		(limit, offset),
		as_dict=True,
	)
	items = []
	for row in rows:
		case = _case(row.name)
		if frappe.db.exists(case.reference_doctype, case.reference_name) and _readable(
			_source(case), frappe.session.user
		):
			items.append(public_case(case))
	return {"items": items, "next_offset": offset + len(rows) if len(rows) == limit else None}


def run_due_sla_cases() -> dict[str, int]:
	"""Dispatch bounded independent jobs so a deadlock cannot roll back other cases."""
	rows = frappe.get_all(
		CASE,
		filters={"status": ("in", ["Open", "Paused"]), "next_run_at": ("<=", now_datetime())},
		fields=["name", "next_run_at"],
		order_by="next_run_at asc, name asc",
		limit=SCHEDULER_BATCH_SIZE,
	)
	for row in rows:
		stamp = str(row.next_run_at)
		frappe.enqueue(
			"medops.hrp_workflow_authorization.services.sla.process_sla_case",
			case_name=row.name,
			scheduled_for=stamp,
			job_id="medops-sla-" + sla_digest([row.name, stamp]),
			deduplicate=True,
			enqueue_after_commit=not frappe.in_test,
			now=frappe.in_test,
		)
	return {"queued": len(rows)}


def process_sla_case(case_name: str, scheduled_for: str | None = None) -> dict[str, int]:
	try:
		CaseAction().execute(
			{"case_name": case_name, "action": "process", "expected_revision": None, "reason": None},
			idempotency_key="sla-schedule-"
			+ sla_digest([case_name, scheduled_for or str(now_datetime())[:16]]),
		)
		return {"processed": 1, "failed": 0}
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError) as exc:
		# Standard Frappe worker restarts this entire single-case transaction, with a bounded retry.
		raise frappe.RetryBackgroundJobError from exc
	except IoneApplicationError as exc:
		case = _case(case_name, lock=True)
		if case.status in {"Completed", "Cancelled"}:
			return {"processed": 0, "failed": 0}
		case.flags.sla_error_only = True
		case.last_error_code = exc.code
		case.last_error_at = now_datetime()
		case.retry_count = min(100, int(case.retry_count or 0) + 1)
		case.next_run_at = now_datetime() + timedelta(minutes=min(60, 2 ** min(6, case.retry_count)))
		_save(case)
		return {"processed": 0, "failed": 1}
