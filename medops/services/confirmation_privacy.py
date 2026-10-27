from __future__ import annotations

import frappe

from medops.common.confirmation_privacy import REDACTED_CONFIRMATION, ConfirmationPayload

CONFIRM_METHODS = frozenset(
	{
		"medops.api.v1.finance.release.confirm",
		"medops.api.v1.finance.approval.confirm",
		"medops.api.v1.finance.reservation.confirm",
	}
)
RPC_PREFIXES = ("/api/method/", "/api/v1/method/", "/api/v2/method/")


def protect_confirmation_request() -> None:
	"""Shield both confirmation RPC bodies before audit, authentication and dispatch."""
	form = frappe.local.form_dict
	request = frappe.local.request
	path = request.path.rstrip("/")
	command = form.get("cmd")
	methods = {command} if isinstance(command, str) else set()
	# Pinned v1 RPC strips any suffix after the first slash for compatibility.
	methods.update(path[len(prefix) :].split("/")[0] for prefix in RPC_PREFIXES if path.startswith(prefix))
	if methods.intersection(CONFIRM_METHODS) and "payload" in form:
		if not isinstance(form["payload"], ConfirmationPayload):
			form["payload"] = ConfirmationPayload(form["payload"])
		# Native telemetry reads Request.json/form independently of form_dict.
		# Dispatch already owns the opaque carrier; diagnostic views get a mask.
		if request.is_json:
			body = request.get_json(silent=True)
			if isinstance(body, dict) and "payload" in body:
				body["payload"] = REDACTED_CONFIRMATION
		elif "payload" in request.form:
			request.form = request.form.copy()
			request.form["payload"] = REDACTED_CONFIRMATION
