"""Actual standard-controller atomic Commit and encrypted idempotency in disposable CI."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import frappe

from medops.common.budget_ledger import LEDGER
from medops.common.confirmation_privacy import ConfirmationPayload
from medops.common.error_catalog import IoneApplicationError
from medops.common.reservation_commitment_request import build_commitment_post, build_commitment_prepare
from medops.hrp_budget.doctype.hrp_budget_reservation_event.hrp_budget_reservation_event import (
	HRPBudgetReservationEvent,
)
from medops.hrp_budget.services import reservation_commitment_confirmation as confirmation
from medops.hrp_budget.services import reservation_release_state
from medops.hrp_budget.services.reservation_commitment_write import (
	PostReservationCommitment,
	PrepareReservationCommitment,
)
from medops.tests.disposable_reservation_site import require_disposable_reservation_site

DOCTYPE = "HRP Budget Commitment Authorization"
EVENT = "HRP Budget Reservation Event"
ROOT = "HRP Budget Reservation"


def exercise_commitment_post(test, reservation, order, *, amount, clock):
	"""Synthetic session/acknowledgement; no human HTTP or production acceptance claim."""
	require_disposable_reservation_site()
	primary = frappe.local.db
	primary.rollback()
	root = frappe.get_doc(ROOT, reservation)
	frozen = root.as_dict().copy()
	financial = {
		name: frappe.db.count(name) for name in (LEDGER, "GL Entry", "Stock Ledger Entry", ROOT, "Error Log")
	}
	event_count = frappe.db.count(EVENT)
	decisions = frappe.db.count(DOCTYPE, {"event_kind": "Decision"})
	primary.rollback()
	preview_command = build_commitment_prepare(
		{
			"reservation": reservation,
			"purchase_order": order.name,
			"purchase_order_line": order.items[0].name,
			"amount": amount,
			"reason": "Disposable CI explicit commitment conversion",
		}
	)
	preview_key = "ci-commitment-preview-" + uuid4().hex
	post_key = "ci-commitment-post-" + uuid4().hex
	with (
		patch.dict(frappe.conf, {"medops_internal_commitment_confirmation_enabled": True}),
		patch.object(confirmation, "now_datetime", return_value=clock),
	):
		preview = PrepareReservationCommitment().execute(preview_command, idempotency_key=preview_key)
		test.assertFalse(preview.idempotency_replayed)
		primary.commit()
		repeated = PrepareReservationCommitment().execute(preview_command, idempotency_key=preview_key)
		test.assertTrue(repeated.idempotency_replayed)
		test.assertEqual(preview.result, repeated.result)
		primary.rollback()
		proof = preview.result["proof"]
		for value in frappe.get_all("HRP Service Idempotency", pluck="response_snapshot"):
			if str(proof) in str(value):
				raise AssertionError("raw commitment proof persisted in unencrypted idempotency result")
		primary.rollback()
		command = build_commitment_post(
			ConfirmationPayload(
				{
					"confirmation": preview.result["confirmation"],
					"proof": proof,
					"acknowledge": True,
				}
			)
		)
		with patch.dict(frappe.conf, {"medops_internal_commitment_write_enabled": False}):
			with test.assertRaises(IoneApplicationError) as disabled:
				PostReservationCommitment().execute(command, idempotency_key=post_key)
		test.assertEqual(disabled.exception.definition.key, "OPERATION_NOT_ALLOWED")
		primary.rollback()
		with patch.dict(frappe.conf, {"medops_internal_commitment_write_enabled": True}):
			decision_observed = []

			def fail_before_event(doc):
				if doc.kind != "Commit":
					raise AssertionError("unexpected non-Commit event in exact transaction")
				decision_observed.append(
					frappe.db.count(DOCTYPE, {"event_kind": "Decision"}) == decisions + 1
				)
				raise RuntimeError("synthetic after-Decision failure")

			with patch.object(HRPBudgetReservationEvent, "before_insert", fail_before_event):
				with test.assertRaises(IoneApplicationError):
					PostReservationCommitment().execute(command, idempotency_key=post_key)
				test.assertEqual(decision_observed, [True])
				primary.rollback()
			test.assertEqual(frappe.db.count(EVENT), event_count)
			test.assertEqual(frappe.db.count(DOCTYPE, {"event_kind": "Decision"}), decisions)
			primary.rollback()
			expiry_observed = []

			def expire_after_event():
				if frappe.db.count(EVENT) > event_count:
					expiry_observed.append(True)
					return clock + timedelta(minutes=6)
				return clock

			with patch.object(confirmation, "now_datetime", side_effect=expire_after_event):
				with test.assertRaises(IoneApplicationError) as expired:
					PostReservationCommitment().execute(command, idempotency_key=post_key)
				test.assertEqual(expired.exception.definition.key, "PERMISSION_DENIED")
				test.assertTrue(expiry_observed)
				primary.rollback()
			test.assertEqual(frappe.db.count(EVENT), event_count)
			test.assertEqual(frappe.db.count(DOCTYPE, {"event_kind": "Decision"}), decisions)
			primary.rollback()
			posted = PostReservationCommitment().execute(command, idempotency_key=post_key)
			test.assertFalse(posted.idempotency_replayed)
			primary.commit()
			test.assertEqual(frappe.db.count(EVENT), event_count + 1)
			test.assertEqual(frappe.db.count(DOCTYPE, {"event_kind": "Decision"}), decisions + 1)
			test.assertEqual(frappe.get_doc(EVENT, posted.result["event"]).kind, "Commit")
			primary.rollback()
			with patch.object(confirmation, "now_datetime", return_value=clock + timedelta(minutes=6)):
				replay = PostReservationCommitment().execute(command, idempotency_key=post_key)
				test.assertTrue(replay.idempotency_replayed)
				test.assertEqual(replay.result, posted.result)
				primary.rollback()
			test.assertEqual(frappe.db.count(EVENT), event_count + 1)
			test.assertEqual(frappe.db.count(DOCTYPE, {"event_kind": "Decision"}), decisions + 1)
			test.assertEqual(frozen, frappe.get_doc(ROOT, reservation).as_dict())
			test.assertEqual(financial, {name: frappe.db.count(name) for name in financial})
			primary.rollback()
			state = reservation_release_state.inspect_reservation_release(reservation)
			test.assertEqual(Decimal(state.position.open_commitment), Decimal(amount))
			test.assertEqual(
				Decimal(state.position.open_reservation) + Decimal(state.position.open_commitment),
				Decimal(state.position.reserved)
				- Decimal(state.position.released)
				+ Decimal(state.position.committed),
			)
			primary.rollback()
	print(
		"MEDOPS_COMMITMENT_POST_CI:actual Decision+Commit atomic rollback/expiry/encrypted preview replay/posted replay after expiry passed;synthetic acknowledgement",
		flush=True,
	)
