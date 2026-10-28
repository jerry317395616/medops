"""Committed-data multi-process probes, restricted to a fresh disposable CI site.

This module is invoked explicitly; it is not part of ordinary test discovery.
The runner discards the entire site. Immutable synthetic evidence is retained.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import IO, Any
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import add_days, get_bench_path, now_datetime, set_request

from medops.common.budget_control import KEY_FIELDS, budget_control_key
from medops.common.budget_ledger import LEDGER
from medops.common.confirmation_privacy import ConfirmationPayload
from medops.common.exact_money import exact_decimal_amount
from medops.common.material_source_snapshot import material_source_snapshot
from medops.common.reservation_authorization import reservation_intent
from medops.common.reservation_renewal_request import (
	RenewedReservationPostCommand,
	build_renewal_command,
	build_renewed_reservation_post,
)
from medops.hrp_budget.services import (
	reservation_confirmation,
	reservation_renewal_control,
	reservation_renewal_write,
)
from medops.hrp_budget.services.material_request_source import locked_material_request_source
from medops.tests.disposable_reservation_site import PROTOCOL_PREFIX, require_disposable_reservation_site
from medops.tests.test_budget_release_execution import TestBudgetReleaseExecution


@dataclass(frozen=True, slots=True)
class Scenario:
	command: RenewedReservationPostCommand
	sid: str
	clock: str


@dataclass(slots=True)
class Worker:
	process: subprocess.Popen
	messages: queue.Queue
	reader: threading.Thread


class TestDisposableReservationConcurrency(TestBudgetReleaseExecution):
	@classmethod
	def setUpClass(cls):
		require_disposable_reservation_site(empty=True)
		super().setUpClass()

	def _make_source(self, row, amount, item_name, unit_name):
		request_date = str(row.period_start)
		warehouse = frappe.db.get_value("Warehouse", {"company": row.company, "is_group": 0}, "name")
		request = frappe.get_doc(
			dict(
				doctype="Material Request",
				company=row.company,
				material_request_type="Purchase",
				transaction_date=request_date,
				schedule_date=add_days(request_date, 1),
				items=[
					dict(
						item_code=item_name,
						qty=1,
						uom=unit_name,
						conversion_factor=1,
						warehouse=warehouse,
						cost_center=row.cost_center,
						expense_account=row.account,
						project=row.project,
						schedule_date=add_days(request_date, 1),
						rate=str(amount),
						amount=str(amount),
					)
				],
			)
		).insert(ignore_permissions=True)
		request.submit()
		connection = frappe.local.db
		connection.commit()
		source = locked_material_request_source(request.name, request.items[0].name, row.organization_unit)
		connection.rollback()
		from dataclasses import asdict

		snapshot, digest = material_source_snapshot(asdict(source))
		values = {
			field: str(row.get(field)) if field in {"period_start", "period_end"} else row.get(field)
			for field in KEY_FIELDS
		}
		return reservation_intent(
			source_snapshot=snapshot,
			source_snapshot_digest=digest,
			allocation="ci-concurrency-allocation",
			key=budget_control_key(values),
			currency=source.currency,
			precision=int(row.currency_precision),
			amount=exact_decimal_amount(str(amount), int(row.currency_precision)),
			budget_date=request_date,
			reason="Disposable CI concurrency probe",
		)

	def _original(self, item, sid):
		frappe.set_user("Administrator")
		set_request(method="POST", path="/")
		frappe.session.sid = sid
		prepared = reservation_confirmation.prepare_reservation_confirmation(item)
		connection = frappe.local.db
		connection.commit()
		decided = reservation_confirmation.confirm_reservation_confirmation(
			prepared["confirmation"], prepared["proof"], True
		)
		connection.commit()
		return prepared["confirmation"], decided["decision"]

	def _renew(self, item, sid, first, original, future):
		from datetime import timedelta

		frappe.set_user("Administrator")
		set_request(method="POST", path="/")
		frappe.session.sid = sid
		with patch.object(reservation_confirmation, "now_datetime", return_value=future):
			prepared = reservation_confirmation.prepare_reservation_confirmation(item)
		connection = frappe.local.db
		connection.commit()
		command = build_renewal_command(
			ConfirmationPayload(
				{
					"original_confirmation": first,
					"original_decision": original,
					"renewal_confirmation": prepared["confirmation"],
					"proof": prepared["proof"],
					"acknowledge": True,
				}
			)
		)
		with (
			patch.object(
				reservation_renewal_write, "now_datetime", return_value=future + timedelta(seconds=1)
			),
			patch.object(
				reservation_renewal_control, "now_datetime", return_value=future + timedelta(seconds=1)
			),
		):
			result = reservation_renewal_write.RenewReservationDecision().execute(
				command,
				idempotency_key="ci-renewal-" + uuid4().hex,
			)
		connection.commit()
		post = build_renewed_reservation_post(
			ConfirmationPayload(
				{
					"original_confirmation": first,
					"original_decision": original,
					"renewal_confirmation": prepared["confirmation"],
					"renewal_decision": result.result["renewal"],
					"proof": prepared["proof"],
					"acknowledge": True,
				}
			)
		)
		return Scenario(
			post, sid, (future + timedelta(seconds=2)).isoformat(sep=" ", timespec="microseconds")
		)

	def _spawn(self, scenario, *, key, hold, retry, errors: IO):
		process = subprocess.Popen(
			[sys.executable, f"{get_bench_path()}/apps/medops/scripts/reservation_concurrency_worker.py"],
			cwd=f"{get_bench_path()}/sites",
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=errors,
			text=True,
			bufsize=1,
		)
		self.workers.append(process)
		messages = queue.Queue()

		def read_messages():
			assert process.stdout is not None
			for line in process.stdout:
				if line.startswith(PROTOCOL_PREFIX):
					messages.put(json.loads(line[len(PROTOCOL_PREFIX) :]))
			messages.put(None)

		reader = threading.Thread(target=read_messages, daemon=True)
		reader.start()
		payload = ConfirmationPayload(
			{
				"site": frappe.local.site,
				"sid": scenario.sid,
				"clock": scenario.clock,
				"idempotency_key": key,
				"hold": hold,
				"retry_conflict": retry,
				"command": {
					"original_confirmation": scenario.command.original_confirmation,
					"original_decision": scenario.command.original_decision,
					"renewal_confirmation": scenario.command.renewal_confirmation,
					"renewal_decision": scenario.command.renewal_decision,
					"proof": scenario.command.proof,
					"acknowledge": True,
				},
			}
		)
		assert process.stdin is not None
		process.stdin.write(json.dumps(payload.unwrap()) + "\n")
		process.stdin.flush()
		return Worker(process, messages, reader)

	def _stage(self, worker, stage, *, timeout=180) -> dict[str, Any]:
		deadline = time.monotonic() + timeout
		while True:
			try:
				message = worker.messages.get(timeout=max(0.01, deadline - time.monotonic()))
			except queue.Empty:
				raise AssertionError("disposable worker did not reach the expected stage") from None
			if message is None:
				raise AssertionError("disposable worker exited before the expected stage")
			if message["stage"] == stage:
				return message
			if message["stage"] == "done":
				raise AssertionError(
					"disposable worker completed before the expected stage: "
					+ str(message.get("status"))
					+ "/"
					+ str(message.get("key") or message.get("error_type"))
				)

	def _signal(self, worker, value):
		assert worker.process.stdin is not None
		worker.process.stdin.write(value + "\n")
		worker.process.stdin.flush()

	def _pair(self, first, second, *, same_key, retry_second, errors):
		key = "ci-post-" + uuid4().hex
		one = self._spawn(first, key=key, hold=True, retry=False, errors=errors)
		two = self._spawn(
			second, key=key if same_key else key + "-second", hold=False, retry=retry_second, errors=errors
		)
		self._stage(one, "ready")
		self._stage(two, "ready")
		self._signal(one, "go")
		self._stage(one, "guard_acquired")
		self._signal(two, "go")
		self._stage(two, "started")
		deadline = time.monotonic() + 1
		while time.monotonic() < deadline:
			try:
				progress = two.messages.get(timeout=max(0.01, deadline - time.monotonic()))
			except queue.Empty:
				break
			if progress is None or progress["stage"] in {"guard_acquired", "done", "retry"}:
				raise AssertionError("competing process passed the held transaction fence")
		self.assertIsNone(two.process.poll())
		self._signal(one, "release")
		result_one = self._stage(one, "done")
		result_two = self._stage(two, "done")
		self.assertEqual(one.process.wait(timeout=30), 0)
		self.assertEqual(two.process.wait(timeout=30), 0)
		one.reader.join(timeout=5)
		two.reader.join(timeout=5)
		return result_one, result_two

	def test_two_process_budget_contention_and_duplicate_receipt_are_safe(self):
		from datetime import timedelta
		from tempfile import TemporaryFile

		require_disposable_reservation_site(empty=True)
		self.assertEqual(self.execute(self.claim())["status"], "Posted")
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		row = frappe.get_doc(
			LEDGER,
			frappe.db.get_value(
				LEDGER, {"voucher_no": self.release, "organization_unit": self.units["A"]}, "name"
			),
		)
		unique = uuid4().hex[:12]
		group = "CI Concurrency Group " + unique
		unit = "CI Concurrency Unit " + unique
		item_name = "CI-CONCURRENCY-" + unique
		if not frappe.db.exists("Item Group", "All Item Groups"):
			frappe.get_doc(dict(doctype="Item Group", item_group_name="All Item Groups", is_group=1)).insert(
				ignore_permissions=True
			)
		frappe.get_doc(
			dict(doctype="Item Group", item_group_name=group, parent_item_group="All Item Groups")
		).insert(ignore_permissions=True)
		frappe.get_doc(dict(doctype="UOM", uom_name=unit)).insert(ignore_permissions=True)
		frappe.get_doc(
			dict(
				doctype="Item",
				item_code=item_name,
				item_name="Synthetic concurrent item",
				item_group=group,
				stock_uom=unit,
				is_stock_item=0,
			)
		).insert(ignore_permissions=True)
		with localcontext() as context:
			context.prec = 48
			large = (Decimal(row.amount) * Decimal("0.60")).quantize(Decimal(1), rounding=ROUND_FLOOR)
			small = (Decimal(row.amount) * Decimal("0.10")).quantize(Decimal(1), rounding=ROUND_FLOOR)
		self.assertGreater(large * 2, Decimal(row.amount))
		self.assertGreater(small, 0)
		items = [self._make_source(row, amount, item_name, unit) for amount in (large, large, small)]
		sids = ["ci-reservation-session-" + uuid4().hex for _ in items]
		originals = [self._original(item, sid) for item, sid in zip(items, sids, strict=True)]
		future = now_datetime() + timedelta(minutes=6)
		scenarios = [
			self._renew(item, sid, *original, future)
			for item, sid, original in zip(items, sids, originals, strict=True)
		]
		frappe.db.rollback()
		before = {
			doctype: frappe.db.count(doctype)
			for doctype in (LEDGER, "GL Entry", "Stock Ledger Entry", "Error Log")
		}
		self.workers = []
		try:
			with TemporaryFile(mode="w+") as errors:
				winner, loser = self._pair(
					scenarios[0], scenarios[1], same_key=False, retry_second=True, errors=errors
				)
				self.assertEqual((winner["status"], winner["replayed"]), ("posted", False))
				self.assertEqual(
					(loser["status"], loser["key"], loser["attempts"]), ("rejected", "CONFLICT", 2)
				)
				self.assertEqual(loser["source_counts"][-1], 2)
				frappe.db.rollback()
				self.assertEqual(frappe.db.count("HRP Budget Reservation"), 1)
				first, replay = self._pair(
					scenarios[2], scenarios[2], same_key=True, retry_second=False, errors=errors
				)
				self.assertEqual((first["status"], first["replayed"]), ("posted", False))
				self.assertEqual((replay["status"], replay["replayed"]), ("posted", True))
				self.assertEqual(first["result"], replay["result"])
			frappe.db.rollback()
			self.assertEqual(frappe.db.count("HRP Budget Reservation"), 2)
			self.assertEqual(frappe.db.count("HRP Budget Reservation Event"), 2)
			self.assertEqual(frappe.db.count("HRP Budget Reservation Event", {"proof_version": 2}), 2)
			self.assertEqual(before, {doctype: frappe.db.count(doctype) for doctype in before})
		finally:
			for process in self.workers:
				if process.poll() is None:
					process.terminate()
					try:
						process.wait(timeout=5)
					except subprocess.TimeoutExpired:
						process.kill()
						process.wait(timeout=5)


def load_tests(loader, tests, pattern):
	"""Run only this disposable probe, excluding imported and inherited cases."""
	del loader, tests, pattern
	return unittest.TestSuite(
		[
			TestDisposableReservationConcurrency(
				"test_two_process_budget_contention_and_duplicate_receipt_are_safe"
			)
		]
	)
