"""One independent process for the disposable-site reservation concurrency probe."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any, cast
from unittest.mock import patch

import frappe
from frappe.utils import set_request

from medops.common.confirmation_privacy import ConfirmationPayload
from medops.common.error_catalog import IoneApplicationError
from medops.common.reservation_release_request import build_release_reservation_post
from medops.common.reservation_renewal_request import build_renewed_reservation_post
from medops.tests.disposable_reservation_site import PROTOCOL_PREFIX, require_disposable_reservation_site


def emit(stage, **values):
	print(PROTOCOL_PREFIX + json.dumps({"stage": stage, **values}, sort_keys=True), flush=True)


def _value(payload: ConfirmationPayload, key: str) -> Any:
	return cast(dict[str, Any], payload.unwrap())[key]


def main():
	payload = ConfirmationPayload(json.loads(sys.stdin.readline()))
	frappe.init(site=_value(payload, "site"))
	frappe.connect()
	try:
		require_disposable_reservation_site()
		from medops.hrp_budget.services import (
			initial_reservation_control,
			material_usage_preview,
			reservation_release_control,
			reservation_release_write,
		)
		from medops.hrp_budget.services import renewed_initial_reservation_write as posting

		frappe.set_user("Administrator")
		set_request(method="POST", path="/")
		sid = _value(payload, "sid")
		if not isinstance(sid, str) or not sid.startswith("ci-reservation-session-"):
			raise ValueError("synthetic session required")
		frappe.session.sid = sid
		clock = datetime.fromisoformat(_value(payload, "clock"))
		if clock.tzinfo is not None:
			raise ValueError("naive synthetic clock required")
		action = _value(payload, "action")
		if action not in {"post_initial", "post_release"}:
			raise ValueError("explicit disposable posting action required")
		key = _value(payload, "idempotency_key")
		hold = _value(payload, "hold") is True
		retry = _value(payload, "retry_conflict") is True
		acquire = material_usage_preview.acquire_material_budget_guards_for_sources
		source_counts = []
		attempt = 0
		held_once = False

		def execute_post():
			body = ConfirmationPayload(_value(payload, "command"))
			if action == "post_initial":
				return posting.PostRenewedInitialReservation().execute(
					build_renewed_reservation_post(body), idempotency_key=key
				)
			return reservation_release_write.PostReservationRelease().execute(
				build_release_reservation_post(body), idempotency_key=key
			)

		def observed_acquire(*args, **kwargs):
			nonlocal held_once
			emit("before_guard", attempt=attempt)
			result = acquire(*args, **kwargs)
			source_counts.append(len(result))
			emit("guard_acquired", attempt=attempt, source_count=len(result))
			if hold and not held_once:
				held_once = True
				if sys.stdin.readline().strip() != "release":
					raise RuntimeError("probe release signal missing")
			return result

		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		emit("ready")
		if sys.stdin.readline().strip() != "go":
			raise RuntimeError("probe start signal missing")
		with (
			patch.object(posting, "now_datetime", return_value=clock),
			patch.object(initial_reservation_control, "now_datetime", return_value=clock),
			patch.object(reservation_release_control, "now_datetime", return_value=clock),
			patch.object(reservation_release_write, "now_datetime", return_value=clock),
			patch.object(
				material_usage_preview,
				"acquire_material_budget_guards_for_sources",
				side_effect=observed_acquire,
			),
		):
			for attempt in range(1, 3 if retry else 2):
				frappe.db.rollback()
				frappe.db.begin()
				emit("started", attempt=attempt)
				try:
					execution = execute_post()
					frappe.db.commit()
					emit(
						"done",
						status="posted",
						attempts=attempt,
						replayed=execution.idempotency_replayed,
						result=execution.result,
						source_counts=source_counts,
					)
					return
				except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
					# Only this outer process owns the complete transaction retry.
					frappe.db.rollback()
					if retry and attempt == 1:
						emit("retry", key="DATABASE_RETRY")
						continue
					emit(
						"done",
						status="rejected",
						attempts=attempt,
						key="DATABASE_RETRY",
						source_counts=source_counts,
					)
					return
				except IoneApplicationError as error:
					frappe.db.rollback()
					if retry and attempt == 1 and error.definition.key == "CONFLICT":
						emit("retry", key=error.definition.key)
						continue
					emit(
						"done",
						status="rejected",
						attempts=attempt,
						key=error.definition.key,
						source_counts=source_counts,
					)
					return
	except Exception as error:
		frappe.db.rollback()
		emit("done", status="error", error_type=type(error).__name__)
		raise SystemExit(1) from None
	finally:
		frappe.db.rollback()
		frappe.destroy()


if __name__ == "__main__":
	main()
