"""Committed actual-cookie commitment API acceptance on the exact disposable CI site."""

from __future__ import annotations

import unittest
from dataclasses import asdict
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal, localcontext
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import add_days, now_datetime

from medops.api.v1.finance import reservation_operations as api
from medops.common.budget_ledger import LEDGER
from medops.common.budget_planning import VALUE
from medops.common.confirmation_privacy import ConfirmationProof
from medops.hrp_budget.services import reservation_commitment_confirmation as confirmation
from medops.services.errors import raise_ione_error
from medops.tests import test_budget_consolidation as fixture
from medops.tests.disposable_reservation_site import require_disposable_reservation_site
from medops.tests.reservation_concurrency_probe import TestDisposableReservationConcurrency

BASE = "medops.api.v1.finance."
OPERATIONS = "reservation_operations."
ROOT = "HRP Budget Reservation"
EVENT = "HRP Budget Reservation Event"
AUTHORIZATION = "HRP Budget Commitment Authorization"


def native_order(reservation):
	require_disposable_reservation_site()
	root = frappe.get_doc(ROOT, reservation)
	request = frappe.get_doc("Material Request", root.source_name)
	item = next(item for item in request.items if item.name == root.source_line)
	if not frappe.db.exists("Supplier Group", "All Supplier Groups"):
		frappe.get_doc(
			dict(doctype="Supplier Group", supplier_group_name="All Supplier Groups", is_group=1)
		).insert(ignore_permissions=True)
	supplier = frappe.get_doc(
		dict(
			doctype="Supplier",
			supplier_name="CI HTTP Supplier " + uuid4().hex[:12],
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
	return order


def exercise_commitment_http(test, row, item_name, unit_name):
	"""Real cookie/ordinary manager/sources/controllers; consent is synthetic, not a real person."""
	require_disposable_reservation_site()
	frappe.set_user("Administrator")
	primary = frappe.local.db
	primary.rollback()
	frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
	frappe.db.begin()
	frappe.get_doc("User", fixture.MANAGER).add_roles("Purchase User", "Stock User")
	primary.commit()
	with localcontext() as context:
		context.prec = 48
		value = (Decimal(str(row.amount)) * Decimal("0.05")).quantize(Decimal(1), rounding=ROUND_FLOOR)
	test.assertGreater(value, 2)
	item = test._make_source(row, value, item_name, unit_name)
	primary.rollback()
	test.login_actor("Administrator")

	def request(method, payload, *, key=None, status=200):
		print("MEDOPS_COMMITMENT_HTTP_CI:" + method, flush=True)
		response = test.post(
			test.method(BASE + method),
			{"payload": payload},
			headers={"Idempotency-Key": key or "ci-commitment-http-" + uuid4().hex},
		)
		test.assertEqual(response.status_code, status, response.get_data(as_text=True))
		primary.rollback()
		if status != 200:
			return {}
		result = response.get_json()["message"]
		if "proof" in result:
			result["proof"] = ConfirmationProof(result["proof"])
		return result

	source = request(
		"reservation.source",
		{
			"request": item_source(item)["request"],
			"line": item_source(item)["line"],
			"organization_unit": item.key.organization_unit,
		},
	)
	prepared = request(
		"reservation.prepare",
		{
			**source,
			"allocation": item.allocation,
			"budget_key": asdict(item.key),
			"currency": item.currency,
			"precision": item.precision,
			"amount": item.amount,
			"budget_date": item.budget_date,
			"reason": "Disposable commitment HTTP initial source",
		},
	)
	command = {"confirmation": prepared["confirmation"], "proof": prepared["proof"], "acknowledge": True}
	decision = request("reservation.confirm", command)
	posted = request(OPERATIONS + "post", {**command, "decision": decision["decision"]})
	reservation = posted["reservation"]
	frappe.set_user("Administrator")
	order = native_order(reservation)
	primary.commit()
	frozen = frappe.get_doc(ROOT, reservation).as_dict()
	financial = {
		name: frappe.db.count(name) for name in (LEDGER, "GL Entry", "Stock Ledger Entry", ROOT, "Error Log")
	}
	before_events = frappe.db.count(EVENT)
	before_decisions = frappe.db.count(AUTHORIZATION, {"event_kind": "Decision"})
	primary.rollback()
	test.login_actor(fixture.MANAGER)
	inspected = request(OPERATIONS + "inspect", {"reservation": reservation})
	before_receipts = frappe.db.count("HRP Service Idempotency")
	listing = request(
		OPERATIONS + "commitment_orders",
		{"reservation": reservation, "source_digest": inspected["source_digest"]},
	)
	test.assertEqual(frappe.db.count("HRP Service Idempotency"), before_receipts)
	test.assertEqual(listing["effect"], "read_only")
	test.assertEqual(
		[(o["order"], o["line"], o["line_number"]) for o in listing["orders"]],
		[(order.name, order.items[0].name, 1)],
	)
	request(
		OPERATIONS + "commitment_orders", {"reservation": reservation, "source_digest": "0" * 64}, status=409
	)
	payload = {
		"reservation": reservation,
		"purchase_order": order.name,
		"purchase_order_line": order.items[0].name,
		"amount": "1",
		"reason": "Synthetic explicit cookie commitment",
	}
	request(OPERATIONS + "prepare_commitment", {**payload, "source_snapshot": "untrusted"}, status=400)
	before_confirmations = frappe.db.count(AUTHORIZATION)
	primary.rollback()
	label_observed = []
	native = api.native_document

	def deny_label(doctype, name, fields, **kwargs):
		if doctype == VALUE and "display_name" in fields:
			label_observed.append(frappe.db.count(AUTHORIZATION) == before_confirmations + 1)
			raise_ione_error("PERMISSION_DENIED")
		return native(doctype, name, fields, **kwargs)

	with patch.object(api, "native_document", side_effect=deny_label):
		request(OPERATIONS + "prepare_commitment", payload, status=403)
	test.assertEqual(label_observed, [True])
	test.assertEqual(frappe.db.count(AUTHORIZATION), before_confirmations)
	test.assertEqual(frappe.db.count("HRP Service Idempotency"), before_receipts)
	primary.rollback()
	preview = request(OPERATIONS + "prepare_commitment", payload)
	test.assertEqual(preview["effect"], "confirmation_only")
	test.assertEqual(preview["review"]["actor"], fixture.MANAGER)
	test.assertIn("设备预算", preview["review"]["budget_subject_label"])
	test.assertEqual(preview["review"]["order"]["line_number"], 1)
	for field in (
		"proof_hash",
		"session_digest",
		"intent_snapshot_json",
		"material_snapshot_json",
		"order_snapshot_json",
		"decision_set_digest",
	):
		test.assertNotIn(field, frappe.as_json(preview))
	proof = {"confirmation": preview["confirmation"], "proof": preview["proof"], "acknowledge": True}
	scope_name = "COD032C-scope-" + fixture.MANAGER.split("@")[0]
	frappe.set_user("Administrator")
	scope = frappe.get_doc("HRP Access Scope", scope_name)
	scope.enabled = 0
	scope.save(ignore_permissions=True)
	primary.commit()
	try:
		request(OPERATIONS + "post_commitment", proof, status=403)
	finally:
		frappe.set_user("Administrator")
		scope = frappe.get_doc("HRP Access Scope", scope_name)
		scope.enabled = 1
		scope.save(ignore_permissions=True)
		primary.commit()
	test.assertEqual(frappe.db.count(EVENT), before_events)
	test.assertEqual(frappe.db.count(AUTHORIZATION, {"event_kind": "Decision"}), before_decisions)
	primary.rollback()
	preview = request(OPERATIONS + "prepare_commitment", payload)
	proof = {"confirmation": preview["confirmation"], "proof": preview["proof"], "acknowledge": True}
	frappe.set_user("Administrator")
	setter = frappe.get_doc(
		dict(
			doctype="Property Setter",
			doctype_or_field="DocField",
			doc_type="Purchase Order",
			field_name="supplier",
			property="permlevel",
			property_type="Int",
			value="9",
		)
	).insert(ignore_permissions=True)
	primary.commit()
	try:
		request(OPERATIONS + "post_commitment", proof, status=403)
	finally:
		frappe.set_user("Administrator")
		frappe.delete_doc("Property Setter", setter.name, ignore_permissions=True)
		primary.commit()
	test.assertEqual(frappe.db.count(EVENT), before_events)
	test.assertEqual(frappe.db.count(AUTHORIZATION, {"event_kind": "Decision"}), before_decisions)
	primary.rollback()
	preview_key = "ci-cookie-commitment-preview-" + uuid4().hex
	preview = request(OPERATIONS + "prepare_commitment", payload, key=preview_key)
	replay_preview = request(OPERATIONS + "prepare_commitment", payload, key=preview_key)
	test.assertTrue(replay_preview["idempotency_replayed"])
	test.assertEqual(replay_preview["confirmation"], preview["confirmation"])
	proof = {"confirmation": preview["confirmation"], "proof": preview["proof"], "acknowledge": True}
	request(OPERATIONS + "post_commitment", {**proof, "acknowledge": False}, status=400)
	post_key = "ci-cookie-commitment-post-" + uuid4().hex
	result = request(OPERATIONS + "post_commitment", proof, key=post_key)
	test.assertEqual(result["effect"], "reservation_committed")
	with patch.object(confirmation, "now_datetime", return_value=now_datetime() + timedelta(minutes=6)):
		repeated = request(OPERATIONS + "post_commitment", proof, key=post_key)
		test.assertTrue(repeated["idempotency_replayed"])
		test.assertEqual(repeated["event"], result["event"])
	test.assertEqual(frappe.db.count(EVENT), before_events + 1)
	test.assertEqual(frappe.db.count(AUTHORIZATION, {"event_kind": "Decision"}), before_decisions + 1)
	test.assertEqual(frappe.get_doc(ROOT, reservation).as_dict(), frozen)
	test.assertEqual(financial, {name: frappe.db.count(name) for name in financial})
	primary.rollback()
	state = request(OPERATIONS + "inspect", {"reservation": reservation})
	test.assertEqual(Decimal(state["position"]["open_commitment"]), Decimal(1))
	test.assertEqual(Decimal(state["position"]["open_reservation"]), Decimal(item.amount) - 1)
	print(
		"MEDOPS_COMMITMENT_HTTP_CI:ordinary manager actual cookie picker/review/scope+field revocation/full outer rollback/Commit/idempotency passed;synthetic acknowledgement",
		flush=True,
	)
	return {"reservation": reservation, "order": order.name, "line": order.items[0].name}


def item_source(item):
	from medops.common.material_source_snapshot import parse_material_source_snapshot

	return parse_material_source_snapshot(item.source_snapshot, expected_digest=item.source_snapshot_digest)


class TestDisposableCommitmentHTTP(TestDisposableReservationConcurrency):
	def _make_commitment_fixture(self):
		"""Build the shared native fixture only on the fresh disposable CI site."""
		require_disposable_reservation_site(empty=True)
		self.assertEqual(self.execute(self.claim())["status"], "Posted")
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		frappe.db.begin()
		row = frappe.get_doc(
			LEDGER,
			frappe.db.get_value(
				LEDGER, {"voucher_no": self.release, "organization_unit": self.units["A"]}, "name"
			),
		)
		unique = uuid4().hex[:12]
		if not frappe.db.exists("Item Group", "All Item Groups"):
			frappe.get_doc(dict(doctype="Item Group", item_group_name="All Item Groups", is_group=1)).insert(
				ignore_permissions=True
			)
		group = "CI HTTP Commitment " + unique
		unit = "CI HTTP Unit " + unique
		item = "CI-HTTP-COMMIT-" + unique
		frappe.get_doc(
			dict(doctype="Item Group", item_group_name=group, parent_item_group="All Item Groups")
		).insert(ignore_permissions=True)
		frappe.get_doc(dict(doctype="UOM", uom_name=unit)).insert(ignore_permissions=True)
		frappe.get_doc(
			dict(
				doctype="Item",
				item_code=item,
				item_name=item,
				item_group=group,
				stock_uom=unit,
				is_stock_item=0,
			)
		).insert(ignore_permissions=True)
		connection = frappe.local.db
		connection.commit()
		return row, item, unit

	def test_real_cookie_ordinary_manager_commitment_workflow(self):
		row, item, unit = self._make_commitment_fixture()
		exercise_commitment_http(self, row, item, unit)


def load_tests(loader, tests, pattern):
	del loader, tests, pattern
	return unittest.TestSuite(
		[TestDisposableCommitmentHTTP("test_real_cookie_ordinary_manager_commitment_workflow")]
	)
