from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from medops.common.core_reports import QUALITY_EVENT, history_write
from medops.common.domain_service import fingerprint_json
from medops.services.errors import raise_ione_error

EPOCH_KEY = fingerprint_json({"kind": "quality_capture_epoch", "schema_version": 1})


def capture_epoch():
	if not frappe.db.table_exists(QUALITY_EVENT):
		return None
	return frappe.db.get_value(QUALITY_EVENT, EPOCH_KEY, "occurred_at")


def append_event(issue, method=None, *, baseline=False):
	del method
	if not capture_epoch():
		return
	key = fingerprint_json({"issue": issue.name, "revision": int(issue.revision)})
	if frappe.db.exists(QUALITY_EVENT, key):
		return
	before = issue.get_doc_before_save()
	previous = frappe.db.get_value(
		QUALITY_EVENT,
		{"issue": issue.name},
		["to_status", "issue_revision"],
		order_by="issue_revision desc",
		as_dict=True,
		for_update=True,
	)
	if baseline:
		kind = "Baseline"
	elif before is None:
		kind = "Detected"
	else:
		if (
			not previous
			or int(previous.issue_revision) != int(before.revision)
			or previous.to_status != before.issue_status
		):
			raise_ione_error("CONFIGURATION_INVALID")
		if before.issue_status != issue.issue_status:
			kind = "Resolved" if issue.issue_status == "Resolved" else "Reopened"
		elif int(issue.occurrence_count) > int(before.occurrence_count):
			kind = "Repeated"
		else:
			kind = "Observed"
	with history_write():
		frappe.get_doc(
			{
				"doctype": QUALITY_EVENT,
				"event_key": key,
				"issue": issue.name,
				"company": issue.company,
				"hospital": issue.hospital,
				"organization_unit": issue.organization_unit,
				"event_type": kind,
				"from_status": None if baseline or before is None else before.issue_status,
				"to_status": issue.issue_status,
				"occurred_at": now_datetime(),
				"issue_revision": issue.revision,
				"occurrence_count": issue.occurrence_count,
				"access_fields": issue.evaluation_read_fields,
			}
		).insert(ignore_permissions=True)


def ensure_quality_history():
	if not frappe.db.table_exists(QUALITY_EVENT):
		return {"baselines": 0}
	with history_write():
		if not frappe.db.exists(QUALITY_EVENT, EPOCH_KEY):
			frappe.get_doc(
				{
					"doctype": QUALITY_EVENT,
					"event_key": EPOCH_KEY,
					"event_type": "Epoch",
					"occurred_at": now_datetime(),
					"issue_revision": 0,
					"occurrence_count": 0,
					"access_fields": "[]",
				}
			).insert(ignore_permissions=True)
	after = ""
	created = 0
	while True:
		rows = frappe.get_all(
			"HRP Data Quality Issue",
			fields=["name"],
			filters={"name": (">", after)},
			order_by="name asc",
			limit=200,
		)
		if not rows:
			break
		for row in rows:
			issue = frappe.get_doc("HRP Data Quality Issue", row.name, for_update=True)
			if not frappe.db.exists(QUALITY_EVENT, {"issue": issue.name}):
				append_event(issue, baseline=True)
				created += 1
		after = rows[-1].name
	return {"baselines": created}
