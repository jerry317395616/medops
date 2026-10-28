"""Containment gate for the committed-data reservation concurrency CI probe."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import frappe
from frappe.utils import get_bench_path

DISPOSABLE_SITE = "hrp-reservation-concurrency.localhost"
PROTOCOL_PREFIX = "MEDOPS_RESERVATION_CI:"


def require_disposable_reservation_site(*, empty=False):
	"""Refuse committed synthetic records outside the fresh named CI site."""
	runner = os.environ.get("RUNNER_TEMP")
	if (
		sys.platform != "linux"
		or os.environ.get("GITHUB_ACTIONS") != "true"
		or os.environ.get("MEDOPS_CI_RESERVATION_CONCURRENCY") != "1"
		or not runner
		or frappe.local.site != DISPOSABLE_SITE
		or frappe.conf.get("medops_ci_disposable_reservation_site") is not True
	):
		raise RuntimeError("reservation concurrency probe requires its disposable CI site")
	bench = Path(get_bench_path()).resolve()
	if not bench.is_relative_to(Path(runner).resolve()) or not (bench / "sites" / DISPOSABLE_SITE).is_dir():
		raise RuntimeError("reservation concurrency Bench must be inside runner temporary storage")
	if empty:
		for doctype in (
			"HRP Budget Reservation",
			"HRP Budget Reservation Event",
			"HRP Budget Reservation Authorization",
			"HRP Budget Reservation Renewal",
			"HRP Budget Reservation Release",
		):
			if frappe.db.count(doctype):
				raise RuntimeError("reservation concurrency probe requires empty evidence tables")
