"""Real standard-controller previews only on the disposable committed CI site."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import add_days, get_datetime, now_datetime, set_request

from medops.common.budget_ledger import LEDGER
from medops.common.commitment_evidence import COMMITMENT_ROW_FIELDS, verify_commitment_evidence_row
from medops.common.error_catalog import IoneApplicationError
from medops.common.exact_money import exact_decimal_amount
from medops.hrp_budget.services import reservation_commitment_confirmation as confirmation
from medops.hrp_budget.services import reservation_release_state
from medops.tests.disposable_reservation_site import require_disposable_reservation_site

ROOT = "HRP Budget Reservation"
EVENT = "HRP Budget Reservation Event"
DOCTYPE = "HRP Budget Commitment Authorization"


def exercise_commitment_confirmation(test, reservation):
	"""Use actual posted roots/masters/MR/PO/table and controller, no service doubles.

	The actor/session and clock are synthetic, as in the surrounding committed
	probe. This is not proof of a real human HTTP confirmation or financial Commit.
	"""
	require_disposable_reservation_site()
	frappe.set_user("Administrator")
	set_request(method="POST", path="/")
	frappe.session.sid = "ci-commitment-confirmation-" + uuid4().hex
	primary = frappe.local.db
	primary.rollback()
	root = frappe.get_doc(ROOT, reservation)
	frozen = {
		name: root.get(name)
		for name in ("source_snapshot_json", "anchor_digest", "event_count", "reserved_amount")
	}
	request = frappe.get_doc("Material Request", root.source_name)
	item = next(item for item in request.items if item.name == root.source_line)
	if not frappe.db.exists("Supplier Group", "All Supplier Groups"):
		frappe.get_doc(
			dict(doctype="Supplier Group", supplier_group_name="All Supplier Groups", is_group=1)
		).insert(ignore_permissions=True)
	supplier = frappe.get_doc(
		dict(
			doctype="Supplier",
			supplier_name="CI Commitment Supplier " + uuid4().hex[:12],
			supplier_type="Company",
			supplier_group="All Supplier Groups",
			country="China",
		)
	).insert(ignore_permissions=True)
	order = frappe.get_doc(
		dict(
			doctype="Purchase Order",
			company=request.company,
			supplier=supplier.name,
			transaction_date=request.transaction_date,
			schedule_date=add_days(request.transaction_date, 1),
			currency=root.currency,
			conversion_rate=1,
			items=[
				dict(
					item_code=item.item_code,
					qty=item.qty,
					rate=item.rate,
					uom=item.uom,
					stock_uom=item.stock_uom or item.uom,
					conversion_factor=1,
					warehouse=item.warehouse,
					cost_center=item.cost_center,
					expense_account=item.expense_account,
					project=item.project,
					schedule_date=add_days(request.transaction_date, 1),
					material_request=request.name,
					material_request_item=item.name,
				)
			],
		)
	).insert(ignore_permissions=True)
	order.submit()
	primary.commit()
	last = frappe.db.get_value(EVENT, {"reservation": reservation}, "occurred_at", order_by="sequence desc")
	clock = max(now_datetime(), get_datetime(last)) + timedelta(minutes=1)
	amount = exact_decimal_amount("1", int(root.currency_precision))
	baseline = frappe.db.count(DOCTYPE)
	financial = {
		name: frappe.db.count(name)
		for name in (
			LEDGER,
			"GL Entry",
			"Stock Ledger Entry",
			ROOT,
			EVENT,
			"HRP Budget Reservation Release",
			"Error Log",
		)
	}
	primary.rollback()

	def prepare():
		return confirmation.prepare_commitment_confirmation(
			reservation,
			order.name,
			order.items[0].name,
			amount=amount,
			reason="Disposable CI independent commitment preview",
		)

	with test.assertRaises(IoneApplicationError):
		prepare()
	primary.rollback()
	with (
		patch.dict(frappe.conf, {"medops_internal_commitment_confirmation_enabled": True}),
		patch.object(confirmation, "now_datetime", return_value=clock),
	):
		rolled_back = prepare()
		test.assertEqual(frappe.db.count(DOCTYPE), baseline + 1)
		test.assertEqual(frappe.db.count(DOCTYPE, {"event_kind": "Decision"}), 0)
		primary.rollback()
		test.assertFalse(frappe.db.exists(DOCTYPE, rolled_back["confirmation"]))
		created = prepare()
		doc = frappe.get_doc(DOCTYPE, created["confirmation"])
		intent, confirmed = verify_commitment_evidence_row(
			{name: doc.get(name) for name in COMMITMENT_ROW_FIELDS}
		)
		test.assertEqual(
			(doc.docstatus, doc.owner, confirmed.event_kind, confirmed.acknowledged),
			(0, "Administrator", "Confirmation", False),
		)
		test.assertEqual(
			(intent.reservation, doc.purchase_order, doc.purchase_order_line),
			(reservation, order.name, order.items[0].name),
		)
		if str(created["proof"]) in str(doc.as_dict()):
			raise AssertionError("raw commitment confirmation proof was persisted")
		primary.commit()
		test.assertEqual(frappe.db.count(DOCTYPE), baseline + 1)
		for method in (
			lambda: doc.save(ignore_permissions=True),
			lambda: doc.db_set("acknowledged", 1),
			lambda: doc.db_insert(),
		):
			with test.assertRaises(IoneApplicationError):
				method()
			primary.rollback()

		def expired_after_insert():
			return clock + (timedelta(minutes=6) if frappe.db.count(DOCTYPE) > baseline + 1 else timedelta(0))

		with patch.object(confirmation, "now_datetime", side_effect=expired_after_insert):
			with test.assertRaises(IoneApplicationError):
				prepare()
			primary.rollback()
		test.assertEqual(frappe.db.count(DOCTYPE), baseline + 1)
		test.assertEqual(frappe.db.count(DOCTYPE, {"event_kind": "Decision"}), 0)
		test.assertEqual(financial, {name: frappe.db.count(name) for name in financial})
		current = frappe.get_doc(ROOT, reservation)
		test.assertEqual(frozen, {name: current.get(name) for name in frozen})
		state = reservation_release_state.inspect_reservation_release(reservation)
		test.assertEqual(state.source.status, "Ordered")
		primary.rollback()
	print(
		"MEDOPS_COMMITMENT_CONFIRMATION_CI:real standard-controller append/commit/rollback/expiry/immutable checks passed;Decision/event writes zero",
		flush=True,
	)
