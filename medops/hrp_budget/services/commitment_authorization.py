"""Private commitment evidence access and physical schema checks; no write lease."""

from __future__ import annotations

import frappe

from medops.common.commitment_evidence import COMMITMENT_ROW_FIELDS
from medops.services.errors import raise_ione_error

DOCTYPE = "HRP Budget Commitment Authorization"
TABLE = "tabHRP Budget Commitment Authorization"


def evidence_permission(doc, user=None, ptype=None, debug=False):
	"""Custom DocPerm cannot turn this protocol into a standard user document."""
	del doc, user, ptype, debug
	return False


def evidence_query(user=None):
	del user
	return "1=0"


def require_commitment_evidence_storage():
	"""Inspect persisted base storage, not a session-local same-name shadow."""
	engine = frappe.db.sql(
		"SELECT ENGINE FROM information_schema.TABLES "
		"WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND TABLE_TYPE='BASE TABLE'",
		(TABLE,),
	)
	columns = frappe.db.sql(
		"SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
		(TABLE,),
	)
	indexes = frappe.db.sql(
		"""SELECT COLUMN_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX FROM information_schema.STATISTICS
		WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s""",
		(TABLE,),
		as_dict=True,
	)
	unique = {}
	for row in indexes:
		if not int(row.NON_UNIQUE):
			unique.setdefault(row.INDEX_NAME, []).append((int(row.SEQ_IN_INDEX), row.COLUMN_NAME))
	unique_columns = {value[0][1] for value in unique.values() if len(value) == 1 and value[0][0] == 1}
	first_columns = {row.COLUMN_NAME for row in indexes if int(row.SEQ_IN_INDEX) == 1}
	if (
		len(engine) != 1
		or engine[0][0] != "InnoDB"
		or not COMMITMENT_ROW_FIELDS <= {row[0] for row in columns}
		or not {"evidence_key", "confirmation", "one_time_key", "event_digest"} <= unique_columns
		or not {"reservation", "purchase_order", "commitment_identity", "company", "budget_subject"}
		<= first_columns
	):
		raise_ione_error("CONFIGURATION_INVALID")
