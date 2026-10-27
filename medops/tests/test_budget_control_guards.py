from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_bench_path

from medops.common.budget_control import budget_control_key, budget_guard_names
from medops.common.domain_service import DomainServiceDefinition
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services.control_guards import acquire_budget_guards
from medops.services.domain_service import DomainService

CHILD = r"""
import json, sys, time, traceback
from pathlib import Path
import frappe
from medops.common.budget_control import budget_control_key
from medops.hrp_budget.services.control_guards import acquire_budget_guards

site, values, mode, start_file, done_file, release_file = sys.argv[1:]
connected = False
try:
    frappe.init(site=site, sites_path='.')
    frappe.connect()
    connected = True
    frappe.db.rollback()
    frappe.db.sql('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
    frappe.db.begin()
    key = budget_control_key(json.loads(values))
    if mode == 'hold':
        acquire_budget_guards([key])
        Path(start_file).write_text('ready', encoding='utf-8')
        deadline = time.monotonic() + 40
        while not Path(release_file).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('test release signal not received')
            time.sleep(0.05)
    else:
        Path(start_file).write_text('started', encoding='utf-8')
        for attempt in range(4):
            try:
                acquire_budget_guards([key])
                if mode == 'retry_once' and attempt == 0:
                    raise frappe.QueryDeadlockError('synthetic whole-transaction retry')
                Path(done_file).write_text(str(attempt), encoding='utf-8')
                break
            except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
                # Retry only from a new full transaction. A savepoint would not
                # clear the connection marker or safely restore all locks.
                frappe.db.rollback()
                if attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))
except Exception as error:
    frames = [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
              for frame in traceback.extract_tb(error.__traceback__)[-6:]]
    Path(start_file + '.error').write_text(json.dumps({'type': type(error).__name__, 'frames': frames}), encoding='utf-8')
    raise
finally:
    if connected:
        frappe.db.rollback()
    frappe.destroy()
"""


def synthetic_key(suffix):
	return {
		"company": "synthetic-company",
		"hospital": "synthetic-hospital",
		"organization_unit": "synthetic-unit-" + suffix,
		"fiscal_year": "synthetic-year",
		"budget_subject": "synthetic-subject",
		"funding_source": "synthetic-fund",
		"project": None,
		"period_start": "2026-01-01",
		"period_end": "2026-01-31",
	}


def wait_for_file(path, process, timeout=25, error_file=None):
	deadline = time.monotonic() + timeout
	while not path.exists():
		if process.poll() is not None:
			diagnostic = (
				json.loads(error_file.read_text(encoding="utf-8"))
				if error_file and error_file.exists()
				else None
			)
			raise AssertionError(
				f"synthetic lock process exited before marker: {process.returncode}, {diagnostic}"
			)
		if time.monotonic() >= deadline:
			raise AssertionError("synthetic lock process did not reach marker")
		time.sleep(0.05)


class TestBudgetControlGuards(IntegrationTestCase):
	def test_same_scope_waits_different_unit_proceeds_and_rollbacks_leave_no_guards(self):
		one = synthetic_key(uuid4().hex)
		other = synthetic_key(uuid4().hex)
		overlap = dict(
			one, fiscal_year="synthetic-other-year", period_start="2026-01-15", period_end="2026-02-15"
		)
		names = budget_guard_names([budget_control_key(row) for row in (one, other, overlap)])
		self.assertTrue(all(len(name) <= 100 for name in names))
		for name in names:
			self.assertEqual(
				frappe.db.sql("SELECT COUNT(*) FROM `tabSeries` WHERE `name` = %s", (name,))[0][0], 0
			)
		processes = []
		with tempfile.TemporaryDirectory(prefix="medops-budget-guard-") as temp:
			root = Path(temp)
			release = root / "release"

			def launch(values, mode, label):
				start = root / (label + "-start")
				done = root / (label + "-done")
				process = subprocess.Popen(
					[
						sys.executable,
						"-c",
						CHILD,
						frappe.local.site,
						json.dumps(values),
						mode,
						str(start),
						str(done),
						str(release),
					],
					cwd=os.path.join(get_bench_path(), "sites"),
					stdin=subprocess.DEVNULL,
					stdout=subprocess.DEVNULL,
					stderr=subprocess.DEVNULL,
				)
				processes.append(process)
				return process, start, done, Path(str(start) + ".error")

			try:
				holder, holder_ready, _, holder_error = launch(one, "hold", "holder")
				wait_for_file(holder_ready, holder, error_file=holder_error)
				same, same_started, same_done, same_error = launch(one, "try", "same")
				overlapping, overlap_started, overlap_done, overlap_error = launch(overlap, "try", "overlap")
				different, different_started, different_done, different_error = launch(
					other, "retry_once", "different"
				)
				wait_for_file(same_started, same, error_file=same_error)
				wait_for_file(overlap_started, overlapping, error_file=overlap_error)
				wait_for_file(different_started, different, error_file=different_error)
				wait_for_file(different_done, different, error_file=different_error)
				self.assertEqual(different.wait(timeout=10), 0)
				self.assertGreaterEqual(int(different_done.read_text(encoding="utf-8")), 1)
				self.assertIsNone(holder.poll())
				self.assertIsNone(same.poll())
				self.assertIsNone(overlapping.poll())
				self.assertFalse(same_done.exists(), "same-key guard must wait until the holder rolls back")
				self.assertFalse(overlap_done.exists(), "overlapping periods must wait on the common scope")
				release.write_text("release", encoding="utf-8")
				self.assertEqual(holder.wait(timeout=10), 0)
				wait_for_file(same_done, same, error_file=same_error)
				self.assertEqual(same.wait(timeout=10), 0)
				wait_for_file(overlap_done, overlapping, error_file=overlap_error)
				self.assertEqual(overlapping.wait(timeout=10), 0)
			finally:
				release.touch()
				for process in processes:
					if process.poll() is None:
						process.terminate()
						try:
							process.wait(timeout=5)
						except subprocess.TimeoutExpired:
							process.kill()
							process.wait(timeout=5)
		frappe.db.rollback()
		for name in names:
			self.assertEqual(
				frappe.db.sql("SELECT COUNT(*) FROM `tabSeries` WHERE `name` = %s", (name,))[0][0], 0
			)

	def test_incorrect_transaction_mode_is_denied_before_guard_writes(self):
		guard = budget_control_key(synthetic_key(uuid4().hex))
		with patch.object(frappe.db, "sql", side_effect=[[("REPEATABLE-READ", 0)], [("InnoDB",)]]) as sql:
			with self.assertRaises(IoneApplicationError) as denied:
				acquire_budget_guards([guard])
		self.assertEqual(denied.exception.code, "IONE-CORE-0009")
		self.assertEqual(sql.call_count, 2)

	def test_one_shot_guard_survives_savepoint_rollback_and_resets_on_full_boundaries(self):
		first = budget_control_key(synthetic_key(uuid4().hex))
		second = budget_control_key(synthetic_key(uuid4().hex))
		names = budget_guard_names([first, second])
		primary = frappe.local.db
		db = None
		cleanup_allowed = False
		try:
			with self.secondary_connection():
				db = frappe.local.db
				db.rollback()
				db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
				db.begin()
				for name in names:
					self.assertEqual(
						db.sql("SELECT COUNT(*) FROM `tabSeries` WHERE `name` = %s", (name,))[0][0], 0
					)
				cleanup_allowed = True
				db.savepoint("cod034_guard_before")
				acquire_budget_guards([first])
				with self.assertRaises(IoneApplicationError) as duplicate:
					acquire_budget_guards([second])
				self.assertEqual(duplicate.exception.code, "IONE-CORE-0008")
				db.rollback(save_point="cod034_guard_before")
				with self.assertRaises(IoneApplicationError) as after_savepoint:
					acquire_budget_guards([second])
				self.assertEqual(after_savepoint.exception.code, "IONE-CORE-0008")
				db.rollback()
				acquire_budget_guards([second])
				db.rollback()
				acquire_budget_guards([first])
				db.commit()
				self.assertIsNone(getattr(db, "_medops_budget_guard_marker", None))
				acquire_budget_guards([second])
				db.rollback()
		finally:
			if db is not None:
				frappe.local.db = db
				try:
					db.rollback()
					if cleanup_allowed:
						for name in names:
							db.sql("DELETE FROM `tabSeries` WHERE `name` = %s", (name,))
						db.commit()
					db.rollback()
					db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
					db.begin()
				finally:
					frappe.local.db = primary
			else:
				frappe.local.db = primary

	def test_domain_service_failure_rolls_back_whole_guarded_transaction(self):
		guard = budget_control_key(synthetic_key(uuid4().hex))
		guard_names = budget_guard_names([guard])
		before = "medops:guard-test:before:" + uuid4().hex
		after = "medops:guard-test:after:" + uuid4().hex
		names = (*guard_names, before, after)
		primary = frappe.local.db
		previous_user = frappe.session.user
		db = None
		reached = []

		class FailingService(DomainService[bool]):
			definition = DomainServiceDefinition(
				name="hrp_budget.test.guard_failure",
				version=1,
				kind="command",
				required_roles=frozenset({"System Manager"}),
			)

			def request_payload(self, command):
				return {"guarded": command}

			def perform(self, command):
				reached.append(command)
				if command:
					acquire_budget_guards([guard])
				frappe.db.sql("INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, 0)", (after,))
				raise RuntimeError("synthetic service failure")

		try:
			frappe.set_user("Administrator")
			with self.secondary_connection():
				db = frappe.local.db
				db.rollback()
				db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
				db.begin()
				for name in names:
					self.assertEqual(
						db.sql("SELECT COUNT(*) FROM `tabSeries` WHERE `name` = %s", (name,))[0][0], 0
					)
				db.sql("INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, 0)", (before,))
				with self.assertRaises(IoneApplicationError) as unguarded:
					FailingService().execute(False, idempotency_key="COD034-unguarded-" + uuid4().hex)
				self.assertEqual(unguarded.exception.code, "IONE-CORE-0012")
				self.assertEqual(reached, [False])
				self.assertEqual(
					db.sql("SELECT COUNT(*) FROM `tabSeries` WHERE `name` = %s", (before,))[0][0], 1
				)
				self.assertEqual(
					db.sql("SELECT COUNT(*) FROM `tabSeries` WHERE `name` = %s", (after,))[0][0], 0
				)
				db.rollback()
				db.sql("INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, 0)", (before,))
				with self.assertRaises(IoneApplicationError) as guarded:
					FailingService().execute(True, idempotency_key="COD034-guarded-" + uuid4().hex)
				self.assertEqual(guarded.exception.code, "IONE-CORE-0012")
				self.assertEqual(reached, [False, True])
				for name in names:
					self.assertEqual(
						db.sql("SELECT COUNT(*) FROM `tabSeries` WHERE `name` = %s", (name,))[0][0], 0
					)
				self.assertIsNone(getattr(db, "_medops_budget_guard_marker", None))
				acquire_budget_guards([guard])
				db.rollback()
		finally:
			if db is not None:
				frappe.local.db = db
				try:
					db.rollback()
					db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
					db.begin()
				finally:
					frappe.local.db = primary
			else:
				frappe.local.db = primary
			frappe.set_user(previous_user)
