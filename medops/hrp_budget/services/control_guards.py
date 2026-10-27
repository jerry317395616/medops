"""Internal transactional row locks; this module grants no budget authority."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from secrets import token_hex

import frappe

from medops.common.budget_control import BudgetControlKey, budget_guard_names, ordered_budget_keys
from medops.common.domain_service import normalize_sha256
from medops.common.organization import normalize_reference
from medops.services.errors import raise_ione_error

# Frappe's standard MariaDB tabSeries has a 100-character primary key and an
# InnoDB row per name. Guard names contain hashes, not source identifiers.
MAX_BOUND_MATERIAL_SOURCES = 100
MaterialBinding = tuple[str, str, str, str]


@dataclass(slots=True)
class _GuardState:
	invalidated: bool = False


@dataclass(frozen=True, slots=True)
class _GuardMarker:
	keys: tuple[BudgetControlKey, ...]
	witness: str
	source_bindings: tuple[MaterialBinding, ...] = ()
	state: _GuardState = field(default_factory=_GuardState)


def _witness_present(db, marker):
	rows = db.sql("SELECT `current` FROM `tabSeries` WHERE `name` = %s", (marker.witness,))
	return len(rows) == 1 and rows[0][0] == 0


def assert_budget_guards(keys):
	"""Check the same key set and a transaction-local witness before a write.

	This detects rollback to a savepoint older than guard acquisition and a full
	Frappe-managed transaction boundary. It cannot detect direct SQL commits or
	prove sources, permissions or balances. Writers must re-read under the lock.
	"""
	ordered = ordered_budget_keys(keys)
	db = frappe.local.db
	marker = getattr(db, "_medops_budget_guard_marker", None)
	if (
		not isinstance(marker, _GuardMarker)
		or marker.state.invalidated
		or marker.keys != ordered
		or not _witness_present(db, marker)
	):
		raise_ione_error("OPERATION_NOT_ALLOWED")


def invalidate_budget_guards():
	"""Poison an active guard until full rollback, including across savepoints."""
	db = getattr(frappe.local, "db", None)
	marker = getattr(db, "_medops_budget_guard_marker", None)
	if not isinstance(marker, _GuardMarker):
		return False
	marker.state.invalidated = True
	return True


def require_material_source_lock_order(request: str, line: str, unit: str) -> None:
	"""Reject late source locks unless this guard was bound to that source.

	The binding is a cooperating-code ordering check, not an authorization or
	proof against direct SQL, implicit commits, or a forged private marker.
	"""
	marker = getattr(getattr(frappe.local, "db", None), "_medops_budget_guard_marker", None)
	if marker is None:
		return
	if (
		not isinstance(marker, _GuardMarker)
		or marker.state.invalidated
		or not any(binding[:3] == (request, line, unit) for binding in marker.source_bindings)
	):
		invalidate_budget_guards()
		raise_ione_error("OPERATION_NOT_ALLOWED")
	assert_budget_guards(marker.keys)


def assert_material_guard_binding(keys, binding: MaterialBinding) -> None:
	"""Require the source snapshot associated with a live budget guard."""
	assert_budget_guards(keys)
	marker = getattr(frappe.local.db, "_medops_budget_guard_marker", None)
	if not isinstance(marker, _GuardMarker) or binding not in marker.source_bindings:
		invalidate_budget_guards()
		raise_ione_error("OPERATION_NOT_ALLOWED")


def assert_material_guard_bindings(keys, bindings: tuple[MaterialBinding, ...]) -> None:
	"""Require the complete canonical source set associated with a live guard."""
	assert_budget_guards(keys)
	marker = getattr(frappe.local.db, "_medops_budget_guard_marker", None)
	if not isinstance(marker, _GuardMarker) or marker.source_bindings != bindings:
		invalidate_budget_guards()
		raise_ione_error("OPERATION_NOT_ALLOWED")


def _before_guard_commit(db, marker):
	# Frappe clears rollback callbacks before running before_commit. Restore the
	# marker cleanup in case this callback or a later callback fails.
	db.after_rollback.add(lambda: _finish_guard_transaction(db, marker))
	try:
		if (
			getattr(db, "_medops_budget_guard_marker", None) is not marker
			or marker.state.invalidated
			or not _witness_present(db, marker)
		):
			raise_ione_error("OPERATION_NOT_ALLOWED")
		# The pinned Frappe CallbackManager drains this deque in order. Recheck
		# after all other callbacks, including ones they add, so a later callback
		# cannot fail after the witness has been removed.
		pending = getattr(db.before_commit, "_functions", None)
		if not isinstance(pending, deque):
			raise_ione_error("CONFIGURATION_INVALID")
		if pending:
			db.before_commit.add(lambda: _before_guard_commit(db, marker))
			return
		db.sql("DELETE FROM `tabSeries` WHERE `name` = %s", (marker.witness,))
	except BaseException:
		# CallbackManager removes a callback before invoking it. If commit is
		# retried after a failure, it must not silently bypass this check.
		db.before_commit.add(lambda: _before_guard_commit(db, marker))
		raise


def _finish_guard_transaction(db, marker):
	"""Clear only this connection's marker after a full transaction boundary."""
	if getattr(db, "_medops_budget_guard_marker", None) is marker:
		delattr(db, "_medops_budget_guard_marker")


def acquire_budget_guards(keys):
	return acquire_source_bound_budget_guards(keys, source_binding=None)


def acquire_source_bound_budget_guards(keys, *, source_binding: MaterialBinding | None):
	return acquire_multi_source_bound_budget_guards(
		keys, source_bindings=() if source_binding is None else (source_binding,)
	)


def acquire_multi_source_bound_budget_guards(keys, *, source_bindings: tuple[MaterialBinding, ...]):
	"""Acquire same-key exclusive locks until the caller commits or rolls back.

	This internal helper trusts a cooperating caller to have locked any bound
	source first. Every writer, including release and reversal, must use the
	same protocol.
	This preliminary primitive is not connected to a business endpoint; it does
	not establish source completeness, authorization, or usable funds. Callers
	must first resolve every reference to its actual persisted document name;
	lexical normalization alone cannot model the database's collation aliases.
	The transaction must gather its full key set and acquire it once. A second
	call is rejected even after rollback to a savepoint. A transaction-local
	witness can detect rollback before acquisition when explicitly checked or
	at normal Frappe commit. It cannot detect direct SQL transaction control or
	replace complete source, permission and balance validation.
	"""
	ordered = ordered_budget_keys(keys)
	names = budget_guard_names(ordered)
	if not isinstance(source_bindings, tuple) or len(source_bindings) > MAX_BOUND_MATERIAL_SOURCES:
		raise ValueError("bounded material source bindings required")
	identities = set()
	for binding in source_bindings:
		if not isinstance(binding, tuple) or len(binding) != 4:
			raise ValueError("canonical material source binding required")
		for value in binding[:3]:
			if normalize_reference(value, label="material source") != value:
				raise ValueError("canonical material source binding required")
		if normalize_sha256(binding[3], label="source_digest") != binding[3] or binding[:3] in identities:
			raise ValueError("unique canonical material source binding required")
		identities.add(binding[:3])
	if tuple(sorted(source_bindings)) != source_bindings:
		raise ValueError("ordered material source bindings required")
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
	# Install first so even an already-registered callback that fails cannot
	# erase the rollback cleanup. The callback itself defers the final check
	# until every ordinary callback has run. Unknown framework internals fail.
	pending = getattr(db.before_commit, "_functions", None)
	if not isinstance(pending, deque):
		raise_ione_error("CONFIGURATION_INVALID")
	marker = _GuardMarker(ordered, "medops:budget:witness:" + token_hex(16), source_bindings)
	db._medops_budget_guard_marker = marker
	pending.appendleft(lambda: _before_guard_commit(db, marker))
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
	# A unique transactional insert is undone if an older savepoint is rolled
	# back. It contains no budget data and is deleted before a normal commit.
	db.sql("INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, 0)", (marker.witness,))
	return ordered
