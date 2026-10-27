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

from medops.common.budget_control import budget_control_key
from medops.common.error_catalog import IoneApplicationError
from medops.hrp_budget.services.control_guards import GUARD_PREFIX, acquire_budget_guards

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
        acquire_budget_guards([key])
        Path(done_file).write_text('acquired', encoding='utf-8')
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
		"period_index": 1,
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
	def test_same_key_waits_different_key_proceeds_and_rollbacks_leave_no_guards(self):
		one = synthetic_key(uuid4().hex)
		other = synthetic_key(uuid4().hex)
		names = [GUARD_PREFIX + budget_control_key(row).identity for row in (one, other)]
		self.assertTrue(all(len(name) <= 100 for name in names))
		self.assertEqual(
			frappe.db.sql("SELECT COUNT(*) FROM `tabSeries` WHERE `name` IN (%s, %s)", tuple(names))[0][0], 0
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
				different, different_started, different_done, different_error = launch(
					other, "try", "different"
				)
				wait_for_file(same_started, same, error_file=same_error)
				wait_for_file(different_started, different, error_file=different_error)
				wait_for_file(different_done, different, error_file=different_error)
				self.assertEqual(different.wait(timeout=10), 0)
				self.assertIsNone(holder.poll())
				self.assertIsNone(same.poll())
				self.assertFalse(same_done.exists(), "same-key guard must wait until the holder rolls back")
				release.write_text("release", encoding="utf-8")
				self.assertEqual(holder.wait(timeout=10), 0)
				wait_for_file(same_done, same, error_file=same_error)
				self.assertEqual(same.wait(timeout=10), 0)
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
		self.assertEqual(
			frappe.db.sql("SELECT COUNT(*) FROM `tabSeries` WHERE `name` IN (%s, %s)", tuple(names))[0][0], 0
		)

	def test_incorrect_transaction_mode_is_denied_before_guard_writes(self):
		guard = budget_control_key(synthetic_key(uuid4().hex))
		with patch.object(frappe.db, "sql", side_effect=[[("REPEATABLE-READ", 0)], [("InnoDB",)]]) as sql:
			with self.assertRaises(IoneApplicationError) as denied:
				acquire_budget_guards([guard])
		self.assertEqual(denied.exception.code, "IONE-CORE-0009")
		self.assertEqual(sql.call_count, 2)
