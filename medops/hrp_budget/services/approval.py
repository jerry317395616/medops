from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import asdict
from datetime import timedelta

import frappe
from frappe.model.workflow import apply_workflow
from frappe.utils import get_datetime, now_datetime, today
from frappe.utils.password import get_encryption_key

from medops.common.budget_workflow import (
	APPROVER_ROLE,
	CASE,
	CONFIRMATION_SECONDS,
	REVIEW,
	STATES,
	WORKFLOW_ACTIONS,
	CaseMutation,
	CaseRequest,
	HumanConfirmation,
	ReviewIntent,
	Vote,
	approval_write,
	case_identity,
	parse_decision,
	replay_votes,
	vote_identity,
)
from medops.common.domain_service import DomainServiceDefinition, canonical_json, fingerprint_json
from medops.hrp_budget.services.approval_access import (
	can_approve_case,
	case_query_scope,
	forbidden_actors,
	get_case,
	review_permission,
	submission_preparer,
)
from medops.hrp_budget.services.approval_evidence import (
	decision_history,
	evidence_values,
	progress_for,
	validate_evidence,
)
from medops.hrp_budget.services.approval_native import sync_tasks
from medops.hrp_budget.services.planning import bare_result, execution_payload, lock_hospital
from medops.hrp_budget.services.planning_access import READ_ROLES, WRITE_ROLES
from medops.hrp_budget.services.submission_access import get_submission
from medops.hrp_workflow_authorization.services.approval_matrix import resolve_budget_case_approval
from medops.hrp_workflow_authorization.services.business_rules import enforce_business_rules
from medops.hrp_workflow_authorization.services.segregation import evaluate_budget_case_segregation
from medops.services.audit_context import emit_audit_event, service_audit_scope
from medops.services.data_access import require_scope
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error, require_authenticated_user, require_roles

ACTION_ROLES = WRITE_ROLES | {APPROVER_ROLE}


def require_web_session():
	request = getattr(frappe.local, "request", None)
	if (
		not request
		or request.method != "POST"
		or request.headers.get("Authorization")
		or not frappe.session.sid
		or frappe.session.sid == "Guest"
	):
		raise_ione_error("PERMISSION_DENIED")


def session_digest(case: str) -> str:
	require_web_session()
	return hmac.new(
		get_encryption_key().encode(),
		canonical_json([case, frappe.session.user, frappe.session.sid]).encode(),
		hashlib.sha256,
	).hexdigest()


def require_case_scope(doc, action: str):
	require_scope(
		CASE,
		company=doc.company,
		hospital=doc.hospital,
		organization_unit=doc.organization_unit,
		action=action,
	)


def assert_history(case, events):
	progress = progress_for(case, events)
	if (
		len(events) != int(case.revision) - 1
		or progress.status != case.status
		or progress.current_step != int(case.current_step)
	):
		raise_ione_error("CONFIGURATION_INVALID")
	return progress


def assert_actor(case, action: str, sequence: int):
	require_case_scope(case, "write")
	if action == "Withdraw":
		if case.requested_by != frappe.session.user and not {
			"System Manager",
			"HRP System Manager",
			"HRP Budget Manager",
		}.intersection(frappe.get_roles()):
			raise_ione_error("PERMISSION_DENIED")
		return
	if not can_approve_case(frappe.session.user, case):
		raise_ione_error("PERMISSION_DENIED")
	decision = parse_decision(case.decision_snapshot)
	if not 1 <= sequence <= len(decision["steps"]) or frappe.session.user not in {
		row["user"] for row in decision["steps"][sequence - 1]["approvers"]
	}:
		raise_ione_error("PERMISSION_DENIED")


def assert_current_policy(case):
	decision = resolve_budget_case_approval(case)
	if decision["decision_digest"] != case.decision_digest:
		raise_ione_error("CONFLICT")
	return decision


def sod_proof(case, action: str, sequence: int):
	if action == "Withdraw":
		return fingerprint_json(
			[case.name, case.source_digest, int(case.revision), frappe.session.user, "Withdraw"]
		)
	decision = parse_decision(case.decision_snapshot)
	principals = {frappe.session.user}
	for row in decision["steps"][sequence - 1]["source_assignments"]:
		if row["user"] == frappe.session.user and row.get("delegated_from"):
			principals.add(row["delegated_from"])
	results = []
	for user in sorted(principals):
		result = evaluate_budget_case_segregation(case, user)
		if not result["allowed"]:
			raise_ione_error("INVALID_STATE_TRANSITION")
		results.append(result["decision_digest"])
	return fingerprint_json(results)


class RequestApproval(DomainService[CaseRequest]):
	definition = DomainServiceDefinition(
		name="hrp_budget.approval.request", version=1, kind="command", required_roles=WRITE_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		require_web_session()

	def request_payload(self, command):
		return asdict(command)

	def validate(self, command):
		require_case_scope(get_submission(command.submission), "create")

	def authorize_result(self, result):
		get_case(str(result["name"]))

	def perform(self, command):
		initial = get_submission(command.submission)
		lock_hospital(initial.hospital)
		source = get_submission(command.submission, lock=True)
		require_case_scope(source, "create")
		if int(source.revision) != command.expected_revision:
			raise_ione_error("CONFLICT")
		rows = frappe.db.sql(
			"SELECT name,round_number,status FROM `tabHRP Budget Approval` WHERE submission=%s ORDER BY round_number DESC LIMIT 1 FOR UPDATE",
			(source.name,),
			as_dict=True,
		)
		if rows and rows[0].status not in {"Rejected", "Withdrawn"}:
			raise_ione_error("CONFLICT")
		if rows:
			previous = get_case(rows[0].name, lock=True)
			assert_history(previous, decision_history(previous, lock=True))
		round_number = int(rows[0].round_number) + 1 if rows else 1
		name = case_identity(source.name, round_number)
		with approval_write(CaseMutation("open", name, 0)):
			values = {
				key: source.get(key)
				for key in (
					"company",
					"hospital",
					"organization_version",
					"organization_unit",
					"cost_center",
					"fiscal_year",
					"currency",
					"currency_precision",
					"subject",
					"total_amount",
					"applicant",
				)
			}
			case = frappe.get_doc(
				dict(
					doctype=CASE,
					name=name,
					identity_key=name,
					submission=source.name,
					round_number=round_number,
					active_key=source.name,
					**values,
					source_digest=source.snapshot_digest,
					source_applicant=submission_preparer(source),
					requested_by=frappe.session.user,
					posting_date=today(),
					requested_at=now_datetime(),
					workflow_state=STATES["Pending"],
					status="Pending",
					revision=1,
					current_step=1,
				)
			)
			case.set("__islocal", 1)
			decision = parse_decision(resolve_budget_case_approval(case))
			case.update(
				dict(
					policy_matrix=decision["matrix"],
					policy_revision=decision["policy_version"],
					policy_digest=decision["policy_digest"],
					decision_digest=decision["decision_digest"],
					decision_snapshot=canonical_json(decision),
				)
			)
			case.insert(ignore_permissions=True)
			sync_tasks(case, {row["user"] for row in decision["steps"][0]["approvers"]})
		emit_audit_event(
			"budget_approval_requested",
			logger_name="medops.budget",
			round_number=round_number,
			step_count=len(decision["steps"]),
		)
		return {**bare_result(case), "status": case.status}


def checked_confirmation(name: str, proof: str):
	if not frappe.db.exists(REVIEW, name):
		raise_ione_error("RESOURCE_NOT_FOUND")
	doc = frappe.get_doc(REVIEW, name)
	validate_evidence(doc)
	if (
		doc.event_kind != "Confirmation"
		or doc.actor != frappe.session.user
		or not hmac.compare_digest(str(doc.confirmation_hash), hashlib.sha256(proof.encode()).hexdigest())
		or not hmac.compare_digest(str(doc.session_digest), session_digest(doc.case))
	):
		raise_ione_error("PERMISSION_DENIED")
	return doc


def append_review(
	case, kind, action, reason, sod, proof_hash, session_hash, expiry, *, name=None, confirmation=None
):
	values = dict(
		doctype=REVIEW,
		case=case.name,
		company=case.company,
		hospital=case.hospital,
		organization_unit=case.organization_unit,
		event_kind=kind,
		action=action,
		sequence_no=int(case.current_step),
		actor=frappe.session.user,
		case_revision=int(case.revision),
		source_digest=case.source_digest,
		policy_digest=case.policy_digest,
		decision_digest=case.decision_digest,
		sod_digest=sod,
		reason=reason,
		recorded_at=now_datetime(),
		expires_at=expiry,
		confirmation_hash=proof_hash,
		session_digest=session_hash,
		confirmation=confirmation,
		vote_key=vote_identity(case.name, Vote(int(case.current_step), frappe.session.user, action))
		if kind == "Decision"
		else None,
		revision_key=fingerprint_json([case.name, int(case.revision)]) if kind == "Decision" else None,
	)
	doc = frappe.get_doc(values)
	doc.event_digest = fingerprint_json(evidence_values(doc))
	return doc.insert(ignore_permissions=True, set_name=name)


class PrepareHumanDecision(DomainService[ReviewIntent]):
	definition = DomainServiceDefinition(
		name="hrp_budget.approval.prepare", version=1, kind="command", required_roles=ACTION_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		require_web_session()

	def request_payload(self, command):
		return asdict(command)

	def validate(self, command):
		case = get_case(command.case)
		assert_actor(case, command.action, int(case.current_step))

	def authorize_result(self, result):
		row = checked_confirmation(str(result["confirmation"]), str(result["proof"]))
		case = get_case(row.case)
		assert_actor(case, row.action, int(row.sequence_no))
		if (
			case.status != "Pending"
			or int(case.revision) != int(row.case_revision)
			or get_datetime(row.expires_at) <= now_datetime()
		):
			raise_ione_error("CONFLICT")
		if row.action != "Withdraw":
			assert_current_policy(case)
		if sod_proof(case, row.action, int(row.sequence_no)) != row.sod_digest:
			raise_ione_error("CONFLICT")

	def perform(self, command):
		with approval_write(CaseMutation("intent", command.case, command.expected_revision)):
			case = get_case(command.case, lock=True)
			assert_actor(case, command.action, int(case.current_step))
			events = decision_history(case, lock=True)
			assert_history(case, events)
			if case.status != "Pending" or int(case.revision) != command.expected_revision:
				raise_ione_error("CONFLICT")
			if command.action != "Withdraw":
				assert_current_policy(case)
			try:
				replay_votes(
					parse_decision(case.decision_snapshot),
					(
						*(Vote(int(row.sequence_no), row.actor, row.action) for row in events),
						Vote(int(case.current_step), frappe.session.user, command.action),
					),
					forbidden=forbidden_actors(case),
				)
			except ValueError as exc:
				raise_ione_error("INVALID_STATE_TRANSITION", cause=exc)
			proof = secrets.token_urlsafe(32)
			expiry = now_datetime() + timedelta(seconds=CONFIRMATION_SECONDS)
			row = append_review(
				case,
				"Confirmation",
				command.action,
				command.reason,
				sod_proof(case, command.action, int(case.current_step)),
				hashlib.sha256(proof.encode()).hexdigest(),
				session_digest(case.name),
				expiry,
			)
		return dict(
			confirmation=row.name,
			proof=proof,
			expires_at=str(expiry),
			case=case.name,
			case_revision=int(case.revision),
			action=command.action,
			reason=command.reason,
			total_amount=case.total_amount,
			currency=case.currency,
			source_digest=case.source_digest,
		)


class ConfirmHumanDecision(DomainService[HumanConfirmation]):
	definition = DomainServiceDefinition(
		name="hrp_budget.approval.confirm", version=1, kind="command", required_roles=ACTION_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		require_web_session()

	def request_payload(self, command):
		return dict(
			confirmation=command.confirmation,
			proof_hash=hashlib.sha256(command.proof.encode()).hexdigest(),
			acknowledge=command.acknowledge,
		)

	def validate(self, command):
		row = checked_confirmation(command.confirmation, command.proof)
		case = get_case(row.case)
		assert_actor(case, row.action, int(row.sequence_no))
		if row.action != "Withdraw":
			assert_current_policy(case)
			if sod_proof(case, row.action, int(row.sequence_no)) != row.sod_digest:
				raise_ione_error("CONFLICT")

	def authorize_result(self, result):
		get_case(str(result["name"]))

	def perform(self, command):
		original = checked_confirmation(command.confirmation, command.proof)
		# Match request/aggregation lock ordering. The hospital fence serializes
		# final approvals that may share original source plans across proposals.
		initial = get_case(original.case)
		lock_hospital(initial.hospital)
		with approval_write(CaseMutation("confirm", original.case, int(original.case_revision))):
			case = get_case(original.case, lock=True)
			assert_actor(case, original.action, int(original.sequence_no))
			if (
				case.status != "Pending"
				or int(case.revision) != int(original.case_revision)
				or get_datetime(original.expires_at) <= now_datetime()
			):
				raise_ione_error("CONFLICT")
			if original.action != "Withdraw":
				assert_current_policy(case)
			if sod_proof(case, original.action, int(original.sequence_no)) != original.sod_digest:
				raise_ione_error("CONFLICT")
			if original.action == "Approve":
				enforce_business_rules(case, "approve", required=False)
			events = decision_history(case, lock=True)
			assert_history(case, events)
			if frappe.db.get_value(REVIEW, {"confirmation": original.name}, "name", for_update=True):
				raise_ione_error("CONFLICT")
			try:
				progress = replay_votes(
					parse_decision(case.decision_snapshot),
					(
						*(Vote(int(row.sequence_no), row.actor, row.action) for row in events),
						Vote(int(case.current_step), frappe.session.user, original.action),
					),
					forbidden=forbidden_actors(case),
				)
			except ValueError as exc:
				raise_ione_error("INVALID_STATE_TRANSITION", cause=exc)
			if progress.status == "Approved":
				from medops.hrp_budget.services.consolidation_access import assert_unclaimed_approved_sources

				assert_unclaimed_approved_sources(case)
			name = frappe.generate_hash(length=20)
			with approval_write(
				CaseMutation(
					"confirm", case.name, int(case.revision), progress.status, progress.current_step, name
				)
			):
				review = append_review(
					case,
					"Decision",
					original.action,
					original.reason,
					original.sod_digest,
					original.confirmation_hash,
					original.session_digest,
					original.expires_at,
					name=name,
					confirmation=original.name,
				)
				case.flags.ignore_permissions = True
				case = apply_workflow(case, WORKFLOW_ACTIONS[progress.status])
				decision = parse_decision(case.decision_snapshot)
				pending = (
					{row["user"] for row in decision["steps"][progress.current_step - 1]["approvers"]}
					- set(progress.current_voters)
					if progress.status == "Pending"
					else set()
				)
				sync_tasks(case, pending)
		emit_audit_event(
			"budget_human_decision_recorded",
			logger_name="medops.budget",
			case_revision=int(case.revision),
			status=case.status,
			current_step=int(case.current_step),
		)
		return {**bare_result(case), "status": case.status, "review": review.name}


def request_approval(command, *, correlation_id=None, idempotency_key=None):
	return execution_payload(
		RequestApproval().execute(command, correlation_id=correlation_id, idempotency_key=idempotency_key)
	)


def prepare_decision(command, *, correlation_id=None, idempotency_key=None):
	return execution_payload(
		PrepareHumanDecision().execute(
			command, correlation_id=correlation_id, idempotency_key=idempotency_key
		)
	)


def confirm_decision(command, *, correlation_id=None, idempotency_key=None):
	return execution_payload(
		ConfirmHumanDecision().execute(
			command, correlation_id=correlation_id, idempotency_key=idempotency_key
		)
	)


def read_case(name: str):
	with service_audit_scope():
		require_authenticated_user()
		require_roles(READ_ROLES)
		case = get_case(name)
		events = decision_history(case)
		progress = assert_history(case, events)
		for row in events:
			if not frappe.has_permission(REVIEW, "read", doc=row) or not review_permission(row, ptype="read"):
				raise_ione_error("PERMISSION_DENIED")
		decision = parse_decision(case.decision_snapshot)
		return {
			**case.as_public_dict(),
			"steps": [
				{key: step[key] for key in ("sequence_no", "step_name", "approval_mode")}
				| {"approvers": [row["user"] for row in step["approvers"]]}
				for step in decision["steps"]
			],
			"reviews": [
				{
					key: str(row.get(key)) if key == "recorded_at" else row.get(key)
					for key in (
						"name",
						"actor",
						"action",
						"sequence_no",
						"reason",
						"recorded_at",
						"case_revision",
						"event_digest",
					)
				}
				for row in events
			],
			"current_voters": list(progress.current_voters),
		}


def list_cases(filters, *, offset=0, limit=50):
	with service_audit_scope():
		require_authenticated_user()
		require_roles(READ_ROLES)
		if (
			set(filters) - {"name", "company", "hospital", "organization_unit", "submission", "status"}
			or not 0 <= offset <= 5000
			or not 1 <= limit <= 100
		):
			raise_ione_error("INVALID_REQUEST")
		with case_query_scope():
			names = frappe.get_list(
				CASE, filters=filters, pluck="name", limit=5001, order_by="requested_at desc,name asc"
			)
		if len(names) > 5000:
			raise_ione_error("INVALID_REQUEST")
		from medops.hrp_budget.services.approval_access import case_permission

		items = []
		for name in names:
			doc = frappe.get_doc(CASE, name)
			if frappe.has_permission(CASE, "read", doc=doc) and case_permission(doc, ptype="read"):
				items.append(doc.as_public_dict())
		return {
			"items": items[offset : offset + limit],
			"next_offset": offset + limit if len(items) > offset + limit else None,
			"limit": limit,
		}
