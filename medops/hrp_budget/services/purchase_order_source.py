"""Locked ERPNext purchase-order facts linked to a material request line."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

import frappe

from medops.common.budget_planning import MAX_LINES
from medops.common.domain_service import fingerprint_json, normalize_sha256
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.organization import normalize_reference, normalize_required_date
from medops.hrp_budget.services.control_guards import invalidate_budget_guards
from medops.hrp_budget.services.material_request_source import (
	SOURCE_FIELDS,
	MaterialRequestSource,
	locked_material_request_source,
)
from medops.hrp_budget.services.planning import native_document
from medops.hrp_budget.services.planning_access import READ_ROLES
from medops.services.errors import raise_ione_error, require_roles

ORDER_FIELDS = {
	"company",
	"supplier",
	"currency",
	"conversion_rate",
	"transaction_date",
	"status",
	"items.item_code",
	"items.qty",
	"items.uom",
	"items.rate",
	"items.amount",
	"items.base_amount",
	"items.material_request",
	"items.material_request_item",
	"items.cost_center",
	"items.expense_account",
	"items.project",
	"items.is_free_item",
}


@dataclass(frozen=True, slots=True)
class PurchaseOrderSource:
	order: str
	line: str
	order_modified: str
	line_modified: str
	material_request: str
	material_request_line: str
	material_request_digest: str
	company: str
	hospital: str
	organization_unit: str
	cost_center: str
	expense_account: str
	project: str | None
	supplier: str
	currency: str
	transaction_date: str
	status: str
	item_code: str
	uom: str
	quantity: str
	rate: str
	amount: str
	base_amount: str

	def digest(self) -> str:
		return fingerprint_json({"schema_version": 1, "kind": "purchase_order_source", **asdict(self)})

	def commitment_identity(self) -> str:
		"""Identify a PO line across snapshot changes; this does not authorize it."""
		return fingerprint_json(
			{
				"namespace": "budget-purchase-order-commitment-v1",
				"order": _identity(self.order, "purchase_order"),
				"line": _identity(self.line, "purchase_order_line"),
			}
		)


def _identity(value: object, label: str) -> str:
	name = normalize_reference(value, label=label)
	if name != value:
		raise ValueError("canonical purchase identity required")
	return name


def _read_order(order: str, line: str, source: MaterialRequestSource, actor: str) -> PurchaseOrderSource:
	if frappe.session.user != actor:
		raise_ione_error("CONFLICT")
	doc = native_document("Purchase Order", order, ORDER_FIELDS, lock=True)
	if (
		doc.name != order
		or int(doc.docstatus) != 1
		or doc.status not in {"To Receive and Bill", "To Bill", "To Receive"}
		or not 1 <= len(doc.items) <= MAX_LINES
		or doc.company != source.company
		or doc.currency != source.currency
		or not doc.supplier
	):
		raise_ione_error("CONFLICT")
	items = [item for item in doc.items if item.name == line]
	if len(items) != 1:
		raise_ione_error("CONFLICT")
	item = items[0]
	if (
		(item.material_request, item.material_request_item) != (source.request, source.line)
		or (item.item_code, item.uom, item.cost_center, item.expense_account, item.project or None)
		!= (source.item_code, source.uom, source.cost_center, source.expense_account, source.project)
		or not source.expense_account
		or item.is_free_item
	):
		raise_ione_error("CONFLICT")
	rows = frappe.db.sql(
		"""SELECT CAST(po.conversion_rate AS CHAR) AS conversion_rate,
		CAST(item.qty AS CHAR) AS qty, CAST(item.rate AS CHAR) AS rate,
		CAST(item.amount AS CHAR) AS amount, CAST(item.base_amount AS CHAR) AS base_amount
		FROM `tabPurchase Order` po JOIN `tabPurchase Order Item` item ON item.parent=po.name
		WHERE po.name=%s AND item.name=%s AND item.parenttype='Purchase Order'
		AND item.parentfield='items' FOR UPDATE""",
		(order, line),
		as_dict=True,
	)
	if len(rows) != 1:
		raise_ione_error("CONFLICT")
	values = {
		field: exact_decimal_amount(rows[0][field], 9, maximum=MAX_APPROVAL_AMOUNT)
		for field in ("conversion_rate", "qty", "rate", "amount", "base_amount")
	}
	if (
		Decimal(values["conversion_rate"]) != 1
		or Decimal(values["qty"]) <= 0
		or Decimal(values["qty"]) > Decimal(source.quantity)
		or Decimal(values["amount"]) <= 0
		or values["amount"] != values["base_amount"]
	):
		raise_ione_error("CONFLICT")
	return PurchaseOrderSource(
		order=doc.name,
		line=item.name,
		order_modified=str(doc.modified),
		line_modified=str(item.modified),
		material_request=source.request,
		material_request_line=source.line,
		material_request_digest=source.digest(),
		company=source.company,
		hospital=source.hospital,
		organization_unit=source.organization_unit,
		cost_center=source.cost_center,
		expense_account=source.expense_account,
		project=source.project,
		supplier=doc.supplier,
		currency=doc.currency,
		transaction_date=normalize_required_date(doc.transaction_date, label="transaction_date"),
		status=doc.status,
		item_code=item.item_code,
		uom=item.uom,
		quantity=values["qty"],
		rate=values["rate"],
		amount=values["amount"],
		base_amount=values["base_amount"],
	)


def locked_purchase_order_source(
	order: object,
	line: object,
	request: object,
	request_line: object,
	organization_unit: object,
	*,
	expected_request_digest: object,
) -> tuple[MaterialRequestSource, PurchaseOrderSource]:
	"""Lock both native parents before dependent data, then recheck their facts.

	Call only at the beginning of a clean, owned transaction before any source,
	master or budget locks. The caller must roll back the whole transaction on
	failure. This cannot detect arbitrary prior locks or enforce other writers'
	ordering. It does not establish human approval, split-order totals, historic
	data, budget sufficiency, allocation policy, or permission to post anything.
	Only currently submitted, open orders in the request currency are supported.
	"""
	from medops.hrp_budget.services.release_execution import refresh_financial_authority

	try:
		order_name = _identity(order, "purchase_order")
		line_name = _identity(line, "purchase_order_line")
		request_name = _identity(request, "material_request")
		request_line_name = _identity(request_line, "material_request_line")
		unit = _identity(organization_unit, "organization_unit")
		digest = normalize_sha256(expected_request_digest, label="expected_request_digest")
		if digest != expected_request_digest:
			raise ValueError("canonical request digest required")
		if getattr(frappe.local.db, "_medops_budget_guard_marker", None) is not None:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		actor = frappe.session.user
		db = frappe.local.db
		if db.db_type != "mariadb" or db.auto_commit_on_many_writes:
			raise_ione_error("CONFIGURATION_INVALID")
		state = db.sql("SELECT @@tx_isolation, @@autocommit")
		if (
			len(state) != 1
			or str(state[0][0]).upper().replace("_", "-") != "READ-COMMITTED"
			or int(state[0][1]) != 0
		):
			raise_ione_error("CONFIGURATION_INVALID")
		require_roles(READ_ROLES)
		refresh_financial_authority(actor)
		parents = (
			("Material Request", request_name, SOURCE_FIELDS),
			("Purchase Order", order_name, ORDER_FIELDS),
		)
		for lock in (False, True):
			for doctype, name, fields in parents:
				doc = native_document(doctype, name, fields, lock=lock)
				if doc.name != name or frappe.session.user != actor:
					raise_ione_error("CONFLICT")
		source = locked_material_request_source(request_name, request_line_name, unit)
		if source.digest() != digest or source.docstatus != 1:
			raise_ione_error("CONFLICT")
		purchase = _read_order(order_name, line_name, source, actor)
		if locked_material_request_source(request_name, request_line_name, unit) != source:
			raise_ione_error("CONFLICT")
		refresh_financial_authority(actor)
		require_roles(READ_ROLES)
		if _read_order(order_name, line_name, source, actor) != purchase or frappe.session.user != actor:
			raise_ione_error("CONFLICT")
		return source, purchase
	except (TypeError, ValueError, KeyError) as error:
		invalidate_budget_guards()
		raise_ione_error("CONFLICT", cause=error)
	except BaseException:
		invalidate_budget_guards()
		raise
