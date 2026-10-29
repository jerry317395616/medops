"""One explicit actual-cookie PO lifecycle probe on a fresh disposable CI site.

Run this module independently of commitment_http_probe: the latter intentionally
asserts zero native financial posting. This probe submits a synthetic native
Purchase Receipt and Purchase Invoice through ordinary authenticated HTTP APIs.
It never writes submitted progress, GL Entry, Stock Ledger Entry or HRP evidence.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from decimal import Decimal
from uuid import uuid4

import frappe
from frappe.utils import add_days

from medops.common.budget_ledger import LEDGER
from medops.common.confirmation_privacy import ConfirmationProof
from medops.tests import test_budget_consolidation as fixture
from medops.tests.commitment_http_probe import (
	AUTHORIZATION,
	BASE,
	EVENT,
	OPERATIONS,
	ROOT,
	TestDisposableCommitmentHTTP,
	exercise_commitment_http,
)
from medops.tests.disposable_reservation_site import require_disposable_reservation_site

PO_API = "erpnext.buying.doctype.purchase_order.purchase_order."
PO_MAPPER = "erpnext.buying.doctype.purchase_order.mapper."
UPDATE_ITEMS = "erpnext.accounts.services.child_item_update.update_child_qty_rate"
EVIDENCE_TABLES = (
	LEDGER,
	ROOT,
	EVENT,
	AUTHORIZATION,
	"HRP Budget Reservation Authorization",
	"HRP Budget Reservation Renewal",
	"HRP Budget Reservation Release",
)
NATIVE_LEDGERS = ("GL Entry", "Stock Ledger Entry")


def _digest(value):
	"""Compare persisted private evidence without putting its contents in assertions."""
	return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class TestDisposablePurchaseLifecycleHTTP(TestDisposableCommitmentHTTP):
	def _lifecycle_request(self, method, arguments, *, key=None, status=200):
		print("MEDOPS_PURCHASE_LIFECYCLE_HTTP_CI:" + method, flush=True)
		response = self.post(
			self.method(method),
			arguments,
			headers={"Idempotency-Key": key or "ci-purchase-lifecycle-" + uuid4().hex},
		)
		self.lifecycle_connection.rollback()
		body = response.get_json(silent=True) or {}
		# Never interpolate a response body, proof, cookie or private record into
		# failure output. The endpoint and exception class identify the failing step.
		self.assertEqual(response.status_code, status, f"{method}: {body.get('exc_type', 'HTTP error')}")
		if status != 200:
			return {}
		result = body.get("message") or {}
		if isinstance(result, dict) and "proof" in result:
			result["proof"] = ConfirmationProof(result["proof"])
		return result

	def _lifecycle_operation(self, name, payload, **kwargs):
		return self._lifecycle_request(BASE + OPERATIONS + name, {"payload": payload}, **kwargs)

	def _lifecycle_rows(self, doctype):
		return frappe.get_all(doctype, fields=["*"], order_by="name", limit_page_length=0)

	def _lifecycle_evidence(self):
		return {doctype: _digest(self._lifecycle_rows(doctype)) for doctype in EVIDENCE_TABLES}

	def _lifecycle_native_ledgers(self):
		return {
			doctype: {row.name: _digest(row) for row in self._lifecycle_rows(doctype)}
			for doctype in NATIVE_LEDGERS
		}

	def _lifecycle_order(self):
		return frappe.get_doc("Purchase Order", self.lifecycle_refs["order"])

	def _lifecycle_inspect(self):
		return self._lifecycle_operation("inspect", {"reservation": self.lifecycle_refs["reservation"]})

	def _lifecycle_prepare(self, reason):
		return self._lifecycle_operation("prepare_commitment", {**self.lifecycle_payload, "reason": reason})

	def _lifecycle_proof(self, preview):
		return {"confirmation": preview["confirmation"], "proof": preview["proof"], "acknowledge": True}

	def _lifecycle_history_and_picker(self, expected_status, *, selectable):
		self.assertEqual(self._lifecycle_order().status, expected_status)
		inspected = self._lifecycle_inspect()
		self.assertEqual(inspected["position"], self.lifecycle_position)
		listing = self._lifecycle_operation(
			"commitment_orders",
			{
				"reservation": self.lifecycle_refs["reservation"],
				"source_digest": inspected["source_digest"],
			},
		)
		self.assertEqual(listing["effect"], "read_only")
		self.assertEqual(
			[(row["order"], row["line"]) for row in listing["orders"]],
			[(self.lifecycle_refs["order"], self.lifecycle_refs["line"])] if selectable else [],
		)
		replay = self._lifecycle_operation(
			"post_commitment", self.lifecycle_completed_proof, key=self.lifecycle_completed_key
		)
		self.assertTrue(replay["idempotency_replayed"])
		self.assertEqual(replay["event"], self.lifecycle_completed["event"])
		self.assertEqual(replay["commitment_decision"], self.lifecycle_completed["commitment_decision"])
		self.assertEqual(self._lifecycle_evidence(), self.lifecycle_frozen_evidence)

	def _lifecycle_reject_new_commitment(self, unused_proof):
		before_receipts = frappe.db.count("HRP Service Idempotency")
		self._lifecycle_operation("prepare_commitment", self.lifecycle_payload, status=409)
		self._lifecycle_operation("post_commitment", unused_proof, status=409)
		self.assertEqual(self._lifecycle_evidence(), self.lifecycle_frozen_evidence)
		self.assertEqual(frappe.db.count("HRP Service Idempotency"), before_receipts)

	def _lifecycle_deny_source_edits(self):
		order = self._lifecycle_order()
		line = order.items[0]
		changed_rate = str(Decimal(str(line.rate)) + 1)
		before_order = _digest(order.as_dict())
		before_ledgers = self._lifecycle_native_ledgers()
		before_receipts = frappe.db.count("HRP Service Idempotency")
		attempts = (
			(
				UPDATE_ITEMS,
				{
					"parent_doctype": "Purchase Order",
					"parent_doctype_name": order.name,
					"trans_items": [
						{
							"docname": line.name,
							"item_code": line.item_code,
							"qty": line.qty,
							"rate": changed_rate,
							"uom": line.uom,
							"conversion_factor": line.conversion_factor,
							"description": line.description,
							"schedule_date": str(line.schedule_date),
						}
					],
				},
			),
			(
				"frappe.client.set_value",
				{
					"doctype": "Purchase Order Item",
					"name": line.name,
					"fieldname": "rate",
					"value": changed_rate,
				},
			),
			("frappe.client.cancel", {"doctype": "Purchase Order", "name": order.name}),
		)
		for method, arguments in attempts:
			with self.subTest(native_entrypoint=method):
				self._lifecycle_request(method, arguments, status=403)
				self.assertEqual(_digest(self._lifecycle_order().as_dict()), before_order)
				self.assertEqual(self._lifecycle_evidence(), self.lifecycle_frozen_evidence)
				self.assertEqual(self._lifecycle_native_ledgers(), before_ledgers)
				self.assertEqual(frappe.db.count("HRP Service Idempotency"), before_receipts)

	def _lifecycle_native_document(self, mapper, doctype, *, receipt=None):
		"""Map, insert and submit a real draft with the same ordinary user's cookie."""
		order = self._lifecycle_order()
		draft = self._lifecycle_request(PO_MAPPER + mapper, {"source_name": order.name})
		self.assertEqual(draft["doctype"], doctype)
		self.assertEqual(int(draft.get("docstatus", 0)), 0)
		draft["posting_date"] = str(order.transaction_date)
		draft["set_posting_time"] = 1
		draft["posting_time"] = "12:00:00"
		self.assertEqual(len(draft["items"]), 1)
		if receipt:
			self.assertEqual(doctype, "Purchase Invoice")
			draft["bill_no"] = "CI-LIFECYCLE-" + uuid4().hex
			draft["bill_date"] = str(order.transaction_date)
			draft["due_date"] = str(add_days(order.transaction_date, 1))
			# These are real submitted receipt references on a new invoice draft.
			# Normal previous-document validation and posting remain in charge.
			draft["items"][0]["purchase_receipt"] = receipt["name"]
			draft["items"][0]["pr_detail"] = receipt["items"][0]["name"]
		inserted = self._lifecycle_request("frappe.client.insert", {"doc": draft})
		self.assertEqual(inserted["owner"], fixture.MANAGER)
		self.assertEqual(inserted["docstatus"], 0)
		submitted = self._lifecycle_request("frappe.client.submit", {"doc": inserted})
		self.assertEqual(submitted["docstatus"], 1)
		self.assertEqual(submitted["modified_by"], fixture.MANAGER)
		self.assertEqual(frappe.db.get_value(doctype, submitted["name"], "docstatus"), 1)
		return submitted

	def _lifecycle_assert_native_posting(self, before, receipt, invoice):
		allowed = {("Purchase Receipt", receipt["name"]), ("Purchase Invoice", invoice["name"])}
		for doctype in NATIVE_LEDGERS:
			rows = self._lifecycle_rows(doctype)
			current = {row.name: _digest(row) for row in rows}
			self.assertEqual({name: current.get(name) for name in before[doctype]}, before[doctype])
			for row in rows:
				if row.name not in before[doctype]:
					self.assertIn((row.voucher_type, row.voucher_no), allowed)
		invoice_entries = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Purchase Invoice", "voucher_no": invoice["name"], "is_cancelled": 0},
			fields=["name", "debit", "credit"],
		)
		self.assertGreaterEqual(len(invoice_entries), 2)
		self.assertEqual(
			sum(Decimal(str(row.debit)) for row in invoice_entries),
			sum(Decimal(str(row.credit)) for row in invoice_entries),
		)
		# This shared fixture is deliberately a non-stock item. Its receipt and
		# invoice prove native progress/accounting, not a stock valuation scenario.
		self.assertEqual(self._lifecycle_native_ledgers()["Stock Ledger Entry"], before["Stock Ledger Entry"])

	def test_real_cookie_bound_order_lifecycle_and_native_completion(self):
		require_disposable_reservation_site(empty=True)
		row, item_name, unit_name = self._make_commitment_fixture()
		self.lifecycle_refs = exercise_commitment_http(self, row, item_name, unit_name)
		self.lifecycle_connection = frappe.local.db
		frappe.set_user("Administrator")
		# Only this fixture user on the exact disposable CI site gains native
		# purchasing/receiving/accounting roles; no business permission is bypassed.
		frappe.get_doc("User", fixture.MANAGER).add_roles(
			"Purchase Manager", "Stock Manager", "Accounts Manager"
		)
		self.lifecycle_connection.commit()
		self.lifecycle_payload = {
			"reservation": self.lifecycle_refs["reservation"],
			"purchase_order": self.lifecycle_refs["order"],
			"purchase_order_line": self.lifecycle_refs["line"],
			"amount": "1",
			"reason": "Synthetic lifecycle commitment with explicit acknowledgement",
		}
		# exercise_commitment_http leaves the real manager cookie installed. Keep
		# that exact session for all new confirmations and completed receipt replay.
		preview = self._lifecycle_prepare(self.lifecycle_payload["reason"])
		self.assertEqual(preview["review"]["actor"], fixture.MANAGER)
		self.lifecycle_completed_proof = self._lifecycle_proof(preview)
		self.lifecycle_completed_key = "ci-lifecycle-completed-" + uuid4().hex
		self.lifecycle_completed = self._lifecycle_operation(
			"post_commitment", self.lifecycle_completed_proof, key=self.lifecycle_completed_key
		)
		self.assertEqual(self.lifecycle_completed["effect"], "reservation_committed")
		self.lifecycle_position = self._lifecycle_inspect()["position"]
		self.assertEqual(Decimal(self.lifecycle_position["open_commitment"]), Decimal(2))
		unused = self._lifecycle_prepare("Synthetic unused preview before native status changes")
		unused_proof = self._lifecycle_proof(unused)
		remaining = unused["review"]["order_remaining"]
		self.lifecycle_frozen_evidence = self._lifecycle_evidence()
		before_ledgers = self._lifecycle_native_ledgers()
		before_errors = frappe.db.count("Error Log")
		self.assertEqual(before_errors, 0)
		self._lifecycle_deny_source_edits()

		self._lifecycle_request(
			PO_API + "update_status", {"name": self.lifecycle_refs["order"], "status": "Closed"}
		)
		self._lifecycle_history_and_picker("Closed", selectable=False)
		self._lifecycle_reject_new_commitment(unused_proof)
		self._lifecycle_request(
			PO_API + "close_or_unclose_purchase_orders",
			{"names": [self.lifecycle_refs["order"]], "status": "Draft"},
		)
		self._lifecycle_history_and_picker("To Receive and Bill", selectable=True)
		# Reopening must not revive the earlier unused confirmation or release
		# either existing commitment. A fresh native review sees the same capacity.
		self._lifecycle_operation("post_commitment", unused_proof, status=409)
		fresh = self._lifecycle_prepare("Synthetic fresh review after Closed reopen")
		self.assertEqual(fresh["review"]["order_remaining"], remaining)
		unused_proof = self._lifecycle_proof(fresh)
		self.lifecycle_frozen_evidence = self._lifecycle_evidence()

		self._lifecycle_request(
			PO_API + "update_status", {"name": self.lifecycle_refs["order"], "status": "On Hold"}
		)
		self._lifecycle_history_and_picker("On Hold", selectable=False)
		self._lifecycle_reject_new_commitment(unused_proof)
		self._lifecycle_request(
			PO_API + "update_status", {"name": self.lifecycle_refs["order"], "status": "Draft"}
		)
		self._lifecycle_history_and_picker("To Receive and Bill", selectable=True)
		self._lifecycle_operation("post_commitment", unused_proof, status=409)
		fresh = self._lifecycle_prepare("Synthetic fresh review before actual receiving and invoicing")
		self.assertEqual(fresh["review"]["order_remaining"], remaining)
		unused_proof = self._lifecycle_proof(fresh)
		self.lifecycle_frozen_evidence = self._lifecycle_evidence()
		self.assertEqual(self._lifecycle_native_ledgers(), before_ledgers)

		receipt = self._lifecycle_native_document("make_purchase_receipt", "Purchase Receipt")
		self.assertEqual(Decimal(str(self._lifecycle_order().per_received)), Decimal(100))
		self._lifecycle_history_and_picker("To Bill", selectable=True)
		invoice = self._lifecycle_native_document(
			"make_purchase_invoice", "Purchase Invoice", receipt=receipt
		)
		order = self._lifecycle_order()
		self.assertEqual(Decimal(str(order.per_received)), Decimal(100))
		self.assertEqual(Decimal(str(order.per_billed)), Decimal(100))
		self._lifecycle_history_and_picker("Completed", selectable=False)
		self._lifecycle_reject_new_commitment(unused_proof)
		self._lifecycle_assert_native_posting(before_ledgers, receipt, invoice)
		self.assertEqual(frappe.db.count("Error Log"), before_errors)
		print(
			"MEDOPS_PURCHASE_LIFECYCLE_HTTP_CI:ordinary manager actual cookie;bound Update Items/child set_value/cancel denied;Closed/On Hold/reopen history+same-session receipt;native Receipt+Invoice Completed;no HRP financial change;synthetic acknowledgement",
			flush=True,
		)


def load_tests(loader, tests, pattern):
	del loader, tests, pattern
	return unittest.TestSuite(
		[TestDisposablePurchaseLifecycleHTTP("test_real_cookie_bound_order_lifecycle_and_native_completion")]
	)
