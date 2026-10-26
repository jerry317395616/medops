from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any

from xlsxwriter import Workbook

import frappe
from frappe.utils.xlsxutils import make_xlsx

from medops.api.v1.finance.planning import request_guard
from medops.common.budget_ledger import LEDGER_PUBLIC_FIELDS
from medops.common.budget_statement import build_statement_request
from medops.common.domain_service import DomainServiceDefinition
from medops.common.organization import normalize_reference
from medops.hrp_budget.api.statement_reads import statement_connection
from medops.hrp_budget.services.budget_statement import ReadBudgetStatement
from medops.hrp_budget.services.ledger_access import checked_entry, statement_actor
from medops.hrp_budget.services.planning_access import READ_ROLES
from medops.hrp_budget.services.statement_authority import refresh_statement_actor
from medops.hrp_budget.services.statement_permissions import (
	statement_metadata_types,
	statement_permission_stamp,
)
from medops.services.domain_service import DomainService
from medops.services.errors import raise_ione_error


@frappe.whitelist(allow_guest=True, methods=["GET"])
def statement(payload: Any = None, offset: Any = 0, limit: Any = 50, expected_digest: Any = None, **extra):
	with request_guard(extra, READ_ROLES):
		command = build_statement_request(
			payload, offset=offset, limit=limit, expected_digest=expected_digest
		)
		with statement_connection():
			return ReadBudgetStatement().execute(command).result


@frappe.whitelist(allow_guest=True, methods=["GET"])
def export_statement(payload: Any = None, expected_digest: Any = None, **extra):
	with request_guard(extra, READ_ROLES):
		command = build_statement_request(payload, expected_digest=expected_digest, export=True)
		with statement_connection():
			service = ReadBudgetStatement()
			result = service.execute(command).result
			columns = [*LEDGER_PUBLIC_FIELDS, "running_balance"]
			rows = [
				["" if row.get(key) is None else str(row[key]) for key in columns]
				for row in service.book.rows
			]
			data = [
				[
					str(frappe.get_meta("HRP Budget Ledger").get_label(key))
					if key != "running_balance"
					else "累计净下达额"
					for key in columns
				],
				*rows,
			]
			metadata = [["预算下达与冲销账簿", "精确金额以文本保存"], ["账簿摘要", service.book.digest]]
			metadata += [[key, str(value)] for key, value in service.book.context.items()]
			metadata += [[key, str(value)] for key, value in service.book.totals.items()]
			stream = BytesIO()
			with Workbook(
				stream,
				{
					"constant_memory": True,
					"strings_to_formulas": False,
					"strings_to_urls": False,
					"strings_to_numbers": False,
				},
			) as workbook:
				# Preserve exact text without HTML stripping by the standard helper.
				make_xlsx(data, "Data Export", wb=workbook)
				info = workbook.add_worksheet("查询与合计")
				for row_number, row in enumerate(metadata):
					for column_number, value in enumerate(row):
						info.write_string(row_number, column_number, value)
		frappe.response.update(
			type="download",
			content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
			filename="budget-statement-" + str(result["statement_digest"])[:12] + ".xlsx",
			filecontent=stream.getvalue(),
			display_content_as="attachment",
		)


@dataclass(frozen=True, slots=True)
class ReadEntry:
	name: str


class ReadBudgetLedgerEntry(DomainService[ReadEntry]):
	definition = DomainServiceDefinition(
		name="hrp_budget.ledger.entry", version=1, kind="query", required_roles=READ_ROLES
	)

	def authorize(self, command):
		super().authorize(command)
		statement_actor()

	def request_payload(self, command):
		return dict(name=command.name)

	def perform(self, command):
		self.command = command
		self.metadata_types = statement_metadata_types()
		self.stamp = statement_permission_stamp(self.metadata_types)
		return checked_entry(command.name)

	def authorize_result(self, result):
		refresh_statement_actor()
		if (
			checked_entry(self.command.name) != result
			or statement_permission_stamp(self.metadata_types) != self.stamp
		):
			raise_ione_error("CONFLICT")


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_entry(name: Any = None, **extra):
	with request_guard(extra, READ_ROLES):
		command = ReadEntry(normalize_reference(name, label="name"))
		with statement_connection():
			return ReadBudgetLedgerEntry().execute(command).result
