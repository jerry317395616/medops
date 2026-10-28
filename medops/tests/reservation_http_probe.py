"""Actual HTTP financial writes only inside the exact disposable CI probe site."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import get_datetime

from medops.common.confirmation_privacy import ConfirmationProof
from medops.common.domain_service import idempotency_record_name
from medops.hrp_budget.doctype.hrp_budget_reservation_event.hrp_budget_reservation_event import (
	HRPBudgetReservationEvent,
)
from medops.hrp_budget.services import (
	initial_reservation_control,
	initial_reservation_write,
	renewed_initial_reservation_write,
	reservation_confirmation,
	reservation_confirmation_service,
	reservation_release_control,
	reservation_release_write,
	reservation_renewal_control,
	reservation_renewal_write,
)
from medops.hrp_budget.services.reservation_release_write import PostReservationRelease
from medops.services.errors import raise_ione_error
from medops.tests.disposable_reservation_site import require_disposable_reservation_site

BASE = "medops.api.v1.finance."
OPERATIONS = "reservation_operations."
ROOT = "HRP Budget Reservation"
EVENT = "HRP Budget Reservation Event"
RELEASE = "HRP Budget Reservation Release"


def exercise_reservation_http(test, row, amount, item_name, unit_name):
	"""Native sessions, controllers, sources, ledger, commit and rollback; no service substitutes."""
	require_disposable_reservation_site()
	items = [test._make_source(row, amount, item_name, unit_name) for _ in range(2)]
	frappe.db.rollback()
	test.login_actor("Administrator")

	def request(method, payload, *, key=None, status=200) -> dict[str, Any]:
		print("MEDOPS_HTTP_CI:" + method, flush=True)
		response = test.post(
			test.method(BASE + method),
			{"payload": payload},
			headers={"Idempotency-Key": key or "ci-human-http-" + uuid4().hex},
		)
		test.assertEqual(response.status_code, status, response.get_data(as_text=True))
		frappe.db.rollback()
		if status != 200:
			return {}
		result = response.get_json()["message"]
		if "proof" in result:
			result["proof"] = ConfirmationProof(result["proof"])
		return result

	def payload(item):
		facts = items_source(item)
		source = request(
			"reservation.source",
			{
				"request": facts["request"],
				"line": facts["line"],
				"organization_unit": item.key.organization_unit,
			},
		)
		return {
			**source,
			"allocation": item.allocation,
			"budget_key": asdict(item.key),
			"currency": item.currency,
			"precision": item.precision,
			"amount": item.amount,
			"budget_date": item.budget_date,
			"reason": "Disposable actual human HTTP workflow",
		}

	def items_source(item):
		from medops.common.material_source_snapshot import parse_material_source_snapshot

		return parse_material_source_snapshot(
			item.source_snapshot, expected_digest=item.source_snapshot_digest
		)

	def original(item):
		data = payload(item)
		prepared = request("reservation.prepare", data)
		command = {"confirmation": prepared["confirmation"], "proof": prepared["proof"], "acknowledge": True}
		decided = request("reservation.confirm", command)
		return data, {**command, "decision": decided["decision"]}

	before = {doctype: frappe.db.count(doctype) for doctype in (ROOT, EVENT, RELEASE)}
	_, command = original(items[0])
	post_key = "ci-human-initial-" + uuid4().hex
	initial = request(OPERATIONS + "post", command, key=post_key)
	test.assertFalse(initial["idempotency_replayed"])
	test.assertEqual(initial["effect"], "reservation_posted")
	replayed = request(OPERATIONS + "post", command, key=post_key)
	test.assertTrue(replayed["idempotency_replayed"])
	test.assertEqual(initial["event"], replayed["event"])
	root = initial["reservation"]
	frozen = frappe.get_doc(ROOT, root).as_dict()
	frappe.db.rollback()
	inspected = request(OPERATIONS + "inspect", {"reservation": root})
	test.assertEqual(inspected["position"]["open_reservation"], items[0].amount)
	test.assertEqual(inspected["source"]["request"], items_source(items[0])["request"])
	test.assertEqual(set(inspected), {"position", "source", "allocation", "budget_date", "effect"})

	def prepare_release(value):
		prepared = request(
			OPERATIONS + "prepare_release",
			{"reservation": root, "amount": value, "reason": "Synthetic native HTTP release"},
		)
		test.assertEqual(prepared["effect"], "confirmation_only")
		return {"confirmation": prepared["confirmation"], "proof": prepared["proof"], "acknowledge": True}

	# Integer fixtures avoid introducing browser/float arithmetic into exact money.
	partial = str(Decimal(amount) / 2)
	release = prepare_release(partial)
	release_key = "ci-human-release-" + uuid4().hex
	observed = []
	original_insert = HRPBudgetReservationEvent.before_insert

	def fail_after_decision(doc):
		if doc.kind == "Release Reservation":
			observed.append(frappe.db.count(RELEASE, {"reservation": root, "event_kind": "Decision"}))
			raise_ione_error("CONFLICT")
		return original_insert(doc)

	with patch.object(HRPBudgetReservationEvent, "before_insert", new=fail_after_decision):
		request(OPERATIONS + "post_release", release, key=release_key, status=409)
	test.assertEqual(observed, [1])
	test.assertEqual(frappe.db.count(RELEASE, {"reservation": root, "event_kind": "Decision"}), 0)
	test.assertEqual(frappe.db.count(EVENT, {"reservation": root}), 1)
	test.assertFalse(
		frappe.db.exists(
			"HRP Service Idempotency",
			idempotency_record_name(PostReservationRelease.definition.name, release_key),
		)
	)
	first = request(OPERATIONS + "post_release", release, key=release_key)
	test.assertFalse(first["idempotency_replayed"])
	request(OPERATIONS + "post_release", dict(release, acknowledge=False), status=400)
	second = prepare_release(partial)
	request(OPERATIONS + "post_release", second)
	test.assertEqual(
		Decimal(request(OPERATIONS + "inspect", {"reservation": root})["position"]["open_reservation"]), 0
	)
	replayed = request(OPERATIONS + "post_release", release, key=release_key)
	test.assertTrue(replayed["idempotency_replayed"])
	test.assertEqual(replayed["event"], first["event"])
	test.assertTrue(request(OPERATIONS + "post", command, key=post_key)["idempotency_replayed"])
	test.assertEqual(frappe.get_doc(ROOT, root).as_dict(), frozen)
	frappe.db.rollback()

	# A new persisted human review renews the same immutable original intent;
	# the superseded original can no longer create its first event.
	data, original_post = original(items[1])
	prepared = request("reservation.prepare", data)
	renew_command = {
		"original_confirmation": original_post["confirmation"],
		"original_decision": original_post["decision"],
		"renewal_confirmation": prepared["confirmation"],
		"proof": prepared["proof"],
		"acknowledge": True,
	}
	# A confirmation created before expiry cannot be used as a renewal, even
	# if it is only submitted later. Keep that domain restriction unchanged.
	request(OPERATIONS + "renew", renew_command, status=409)
	future = get_datetime(prepared["expires_at"]) + timedelta(seconds=1)
	with ExitStack() as clocks:
		for module in (
			reservation_confirmation,
			reservation_confirmation_service,
			reservation_renewal_control,
			reservation_renewal_write,
			renewed_initial_reservation_write,
			initial_reservation_control,
			initial_reservation_write,
			reservation_release_control,
			reservation_release_write,
		):
			clocks.enter_context(patch.object(module, "now_datetime", return_value=future))
		prepared = request("reservation.prepare", data)
		renew_command.update(renewal_confirmation=prepared["confirmation"], proof=prepared["proof"])
		renewed = request(OPERATIONS + "renew", renew_command)
		test.assertEqual(renewed["effect"], "decision_renewed")
		request(OPERATIONS + "post", original_post, status=403)
		new_post = {**renew_command, "renewal_decision": renewed["renewal"]}
		result = request(OPERATIONS + "post_renewed", new_post)
		test.assertEqual(result["effect"], "reservation_posted")
		root = result["reservation"]
		unused = prepare_release(items[1].amount)
		test.login_actor("Administrator")
		request(OPERATIONS + "post_release", unused, status=403)
	test.assertEqual(frappe.db.count(EVENT, {"reservation": root}), 1)
	test.assertEqual(frappe.db.count(RELEASE, {"reservation": root, "event_kind": "Decision"}), 0)
	test.assertEqual(frappe.db.count(ROOT), before[ROOT] + 2)
	test.assertEqual(frappe.db.count(EVENT), before[EVENT] + 4)
	test.assertEqual(frappe.db.count(RELEASE), before[RELEASE] + 5)
