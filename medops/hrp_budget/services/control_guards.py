"""Internal transactional row locks; this module grants no budget authority."""

from __future__ import annotations

import frappe

from medops.common.budget_control import budget_guard_names, ordered_budget_keys
from medops.services.errors import raise_ione_error

# Frappe's standard MariaDB tabSeries has a 100-character primary key and an
# InnoDB row per name. Guard names contain hashes, not source identifiers.


def _finish_guard_transaction(db, marker):
	"""Clear only this connection's marker after a full transaction boundary."""
	if getattr(db, "_medops_budget_guard_marker", None) is marker:
		delattr(db, "_medops_budget_guard_marker")


def acquire_budget_guards(keys):
	"""Acquire same-key exclusive locks until the caller commits or rolls back.

	Every writer, including release and reversal, must use the same protocol.
	This preliminary primitive is not connected to a business endpoint; it does
	not establish source completeness, authorization, or usable funds. Callers
	must first resolve every reference to its actual persisted document name;
	lexical normalization alone cannot model the database's collation aliases.
	The transaction must gather its full key set and acquire it once. A second
	call is rejected even after rollback to a savepoint, because Frappe does not
	run transaction callbacks for savepoint rollback. This marker cannot detect
	lost locks or invalidate a previously returned result after such a rollback;
	the future outer service must abort the entire transaction on that path.
	"""
	ordered = ordered_budget_keys(keys)
	names = budget_guard_names(ordered)
	db = frappe.local.db
	if getattr(db, "_medops_budget_guard_marker", None) is not None:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	if db.db_type != "mariadb" or db.auto_commit_on_many_writes:
		raise_ione_error("CONFIGURATION_INVALID")
	state = db.sql("SELECT @@tx_isolation, @@autocommit")
	engine = db.sql(
		"SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
		("tabSeries",),
	)
	if (
		len(state) != 1
		or str(state[0][0]).upper().replace("_", "-") != "READ-COMMITTED"
		or int(state[0][1]) != 0
		or len(engine) != 1
		or str(engine[0][0]).upper() != "INNODB"
	):
		raise_ione_error("CONFIGURATION_INVALID")
	marker = object()
	db._medops_budget_guard_marker = marker
	db.after_commit.add(lambda: _finish_guard_transaction(db, marker))
	db.after_rollback.add(lambda: _finish_guard_transaction(db, marker))
	for name in names:
		db.sql(
			"""INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, 0)
			ON DUPLICATE KEY UPDATE `current` = `current`""",
			(name,),
		)
		locked = db.sql("SELECT `current` FROM `tabSeries` WHERE `name` = %s FOR UPDATE", (name,))
		if len(locked) != 1:
			raise_ione_error("CONFIGURATION_INVALID")
	return ordered
