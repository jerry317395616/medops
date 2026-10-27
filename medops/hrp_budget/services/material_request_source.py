"""Locked, permission-checked ERPNext purchase-source facts; no spending authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import frappe

from medops.common.budget_ledger import LEDGER
from medops.common.budget_planning import MAX_LINES
from medops.common.domain_service import fingerprint_json
from medops.common.exact_money import MAX_APPROVAL_AMOUNT, exact_decimal_amount
from medops.common.organization import normalize_reference
from medops.hrp_budget.services.planning import native_document
from medops.hrp_budget.services.planning_access import READ_ROLES
from medops.hrp_master_data.services.budget_dimensions import native_read
from medops.services.data_access import require_scope
from medops.services.errors import raise_ione_error, require_roles

SOURCE_FIELDS = {
	"company",
	"material_request_type",
	"transaction_date",
	"status",
	"items.item_code",
	"items.qty",
	"items.uom",
	"items.rate",
	"items.amount",
	"items.cost_center",
	"items.project",
}
UNIT_FIELDS = {"company", "hospital", "organization_version", "enabled", "valid_from", "valid_to"}
VERSION_FIELDS = {"company", "hospital", "effective_from", "status"}
MAPPING_FIELDS = {
	"company",
	"hospital",
	"organization_version",
	"organization_unit",
	"cost_center",
	"enabled",
	"revision",
}
CENTER_FIELDS = {"company", "disabled", "is_group"}


def _require_actor(actor: str) -> None:
	if frappe.session.user != actor:
		raise_ione_error("CONFLICT")
	state = frappe.db.get_value("User", actor, ["enabled", "user_type"], as_dict=True)
	if not state or not state.enabled or state.user_type != "System User":
		raise_ione_error("PERMISSION_DENIED")
	require_roles(READ_ROLES)


def _require_unit_scope(unit) -> None:
	for doctype in ("HRP Organization Mapping", LEDGER):
		require_scope(doctype, company=unit.company, hospital=unit.hospital, organization_unit=unit.name)


@dataclass(frozen=True, slots=True)
class MaterialRequestSource:
	"""A selected native row and its checked organization mapping, not an approval."""

	request: str
	line: str
	request_modified: str
	line_modified: str
	company: str
	hospital: str
	organization_unit: str
	organization_version: str
	mapping_revision: int
	cost_center: str
	currency: str
	transaction_date: str
	docstatus: int
	status: str
	item_code: str
	uom: str
	project: str | None
	quantity: str
	rate: str
	amount: str

	def digest(self) -> str:
		return fingerprint_json({"schema_version": 1, "kind": "material_request_source", **asdict(self)})


def _date(value: object) -> date:
	try:
		return date.fromisoformat(str(value))
	except (TypeError, ValueError) as error:
		raise_ione_error("CONFIGURATION_INVALID", cause=error)


def locked_material_request_source(
	request: object, line: object, organization_unit: object
) -> MaterialRequestSource:
	"""Read a native purchase line under locks after checking current permissions.

	Amounts are the stored decimal values, fetched as text because the pinned
	Frappe MariaDB connector converts DECIMAL results to float. This internal
	reader does not approve a source, allocate budget, write events, or establish
	the global writer lock order. Its caller must recheck authority and source
	facts under the eventual common budget guard before any business write.
	"""
	from medops.hrp_budget.services.release_execution import refresh_financial_authority

	request_name = normalize_reference(request, label="material_request")
	line_name = normalize_reference(line, label="material_request_line")
	unit_name = normalize_reference(organization_unit, label="organization_unit")
	actor = frappe.session.user
	require_roles(READ_ROLES)
	refresh_financial_authority(actor)
	_require_actor(actor)
	frappe.get_meta("Material Request", cached=False)
	frappe.get_meta("Material Request Item", cached=False)
	if frappe.db.db_type != "mariadb":
		raise_ione_error("CONFIGURATION_INVALID")
	state = frappe.db.sql("SELECT @@autocommit")
	if len(state) != 1 or int(state[0][0]) != 0:
		raise_ione_error("CONFIGURATION_INVALID")
	# The first read checks permission before locking; the second rechecks it
	# on the current parent and child rows acquired by native Frappe locking.
	native_document("Material Request", request_name, SOURCE_FIELDS)
	source = native_document("Material Request", request_name, SOURCE_FIELDS, lock=True)
	unit = native_document("HRP Organization Unit", unit_name, UNIT_FIELDS, lock=True)
	_require_unit_scope(unit)
	version = native_document(
		"HRP Organization Version", unit.organization_version, VERSION_FIELDS, lock=True
	)
	mapping = native_document("HRP Organization Mapping", unit.name, MAPPING_FIELDS, lock=True)
	if (
		source.material_request_type != "Purchase"
		or int(source.docstatus) not in (0, 1)
		or source.status in {"Stopped", "Cancelled"}
		or not unit.enabled
		or int(version.docstatus) != 1
		or version.status != "Published"
		or not mapping.enabled
		or (source.company, unit.hospital, unit.name, unit.organization_version)
		!= (mapping.company, mapping.hospital, mapping.organization_unit, mapping.organization_version)
		or (version.company, version.hospital) != (unit.company, unit.hospital)
		or not 1 <= len(source.items) <= MAX_LINES
	):
		raise_ione_error("CONFLICT")
	transaction_date = _date(source.transaction_date)
	if (
		(version.effective_from and _date(version.effective_from) > transaction_date)
		or (unit.valid_from and _date(unit.valid_from) > transaction_date)
		or (unit.valid_to and _date(unit.valid_to) < transaction_date)
	):
		raise_ione_error("INVALID_STATE_TRANSITION")
	items = [row for row in source.items if row.name == line_name]
	if len(items) != 1 or not items[0].cost_center or not mapping.cost_center:
		raise_ione_error("CONFLICT")
	item = items[0]
	center = native_document("Cost Center", item.cost_center, CENTER_FIELDS, lock=True)
	if (
		center.name != mapping.cost_center
		or center.company != source.company
		or center.disabled
		or center.is_group
	):
		raise_ione_error("CONFLICT")
	company = native_document("Company", source.company, {"default_currency"}, lock=True)
	if not company.default_currency:
		raise_ione_error("CONFIGURATION_INVALID")
	currency = native_document("Currency", company.default_currency, {"name"}, lock=True)
	product = native_document("Item", item.item_code, {"name"}, lock=True)
	project = native_document("Project", item.project, {"name"}, lock=True) if item.project else None
	rows = frappe.db.sql(
		"""SELECT name, CAST(qty AS CHAR) AS qty, CAST(rate AS CHAR) AS rate,
		CAST(amount AS CHAR) AS amount FROM `tabMaterial Request Item`
		WHERE name=%s AND parent=%s AND parenttype='Material Request' AND parentfield='items' FOR UPDATE""",
		(item.name, source.name),
		as_dict=True,
	)
	if len(rows) != 1 or rows[0].name != item.name:
		raise_ione_error("CONFLICT")
	try:
		quantity, rate, amount = (
			exact_decimal_amount(rows[0][field], 9, maximum=MAX_APPROVAL_AMOUNT)
			for field in ("qty", "rate", "amount")
		)
		mapping_revision = int(mapping.revision)
		if mapping_revision < 1:
			raise ValueError("invalid mapping revision")
	except (TypeError, ValueError) as error:
		raise_ione_error("CONFIGURATION_INVALID", cause=error)
	refresh_financial_authority(actor)
	_require_actor(actor)
	frappe.get_meta("Material Request", cached=False)
	frappe.get_meta("Material Request Item", cached=False)
	_require_unit_scope(unit)
	for doc, fields in (
		(source, SOURCE_FIELDS),
		(unit, UNIT_FIELDS),
		(version, VERSION_FIELDS),
		(mapping, MAPPING_FIELDS),
		(center, CENTER_FIELDS),
		(company, {"default_currency"}),
		(currency, {"name"}),
		(product, {"name"}),
	):
		if not native_read(doc, fields, user=actor):
			raise_ione_error("PERMISSION_DENIED")
	if project and not native_read(project, {"name"}, user=actor):
		raise_ione_error("PERMISSION_DENIED")
	return MaterialRequestSource(
		request=source.name,
		line=item.name,
		request_modified=str(source.modified),
		line_modified=str(item.modified),
		company=company.name,
		hospital=unit.hospital,
		organization_unit=unit.name,
		organization_version=version.name,
		mapping_revision=mapping_revision,
		cost_center=center.name,
		currency=currency.name,
		transaction_date=transaction_date.isoformat(),
		docstatus=int(source.docstatus),
		status=source.status,
		item_code=product.name,
		uom=item.uom,
		project=project.name if project else None,
		quantity=quantity,
		rate=rate,
		amount=amount,
	)
