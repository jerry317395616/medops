"""Owned read adapter. Never ends, commits or alters the caller's transaction."""

from __future__ import annotations

from contextlib import contextmanager, suppress

import frappe

from medops.common.budget_statement_access import StatementRead, current_statement_read, statement_read
from medops.hrp_budget.services.statement_authority import refresh_statement_actor
from medops.services.errors import raise_ione_error, require_authenticated_user

_MISSING = object()
_LOCAL_FIELDS = (
	"session",
	"cache",
	"meta_cache",
	"role_permissions",
	"new_doc_templates",
	"user_perms",
	"form_dict",
	"jenv_restricted",
	"jenv_unrestricted",
)


@contextmanager
def statement_connection(*, actor=None):
	require_authenticated_user()
	if current_statement_read() is not None or frappe.conf.db_type != "mariadb":
		raise_ione_error("CONFIGURATION_INVALID")
	principal = actor or frappe.session.user
	primary = frappe.local.db
	saved = {key: getattr(frappe.local, key, _MISSING) for key in _LOCAL_FIELDS}
	fresh = None
	try:
		# set_user in the pinned authority refresher resets SID and form data.
		# Isolate those changes on a copy and restore the original objects below.
		frappe.local.session = frappe._dict(frappe.local.session)
		frappe.local.meta_cache = {}
		frappe.connect(set_admin_as_user=False)
		fresh = frappe.local.db
		if fresh is primary:
			raise_ione_error("CONFIGURATION_INVALID")
		fresh.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		fresh.sql("SET SESSION TRANSACTION READ ONLY")
		fresh.begin()
		with statement_read(StatementRead(principal, fresh)):
			refresh_statement_actor()
			yield
	finally:
		if fresh is not None:
			with suppress(Exception):
				fresh.rollback()
			with suppress(Exception):
				fresh.close()
		frappe.local.db = primary
		for key, value in saved.items():
			if value is _MISSING:
				with suppress(AttributeError):
					delattr(frappe.local, key)
			else:
				setattr(frappe.local, key, value)
