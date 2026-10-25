from __future__ import annotations

from typing import TYPE_CHECKING

from frappe.model.document import Document

from medops.common.core_reports import reference_batch, reference_identity, reference_write_allowed
from medops.common.master_data import MASTER_DATA_TARGET_POLICIES
from medops.services.errors import raise_ione_error


class HRPReferenceLink(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	if TYPE_CHECKING:
		from frappe.types import DF

		company: DF.Link
		enabled: DF.Check
		hospital: DF.Link
		identity_key: DF.Data
		master_data_domain: DF.Link
		organization_unit: DF.Link | None
		revision: DF.Int
		target_doctype: DF.Link
		target_name: DF.DynamicLink
		valid_from: DF.Date
		valid_to: DF.Date | None
	# end: auto-generated types

	def validate(self):
		if not reference_write_allowed():
			raise_ione_error("OPERATION_NOT_ALLOWED")
		try:
			command = reference_batch(
				{
					k: (str(self.get(k)) if self.get(k) else None)
					if k in {"valid_from", "valid_to"}
					else self.get(k)
					for k in ("company", "hospital", "organization_unit", "master_data_domain")
				},
				[
					{
						k: (str(self.get(k)) if self.get(k) else None)
						if k in {"valid_from", "valid_to"}
						else self.get(k)
						for k in ("target_name", "enabled", "valid_from", "valid_to")
					}
				],
			)
			self.update(command["scope"])
			self.update(command["entries"][0])
		except (ValueError, TypeError) as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)
		if self.target_doctype not in MASTER_DATA_TARGET_POLICIES or self.identity_key != reference_identity(
			self.as_public_dict()
		):
			raise_ione_error("CONFIGURATION_INVALID")
		before = self.get_doc_before_save()
		if before and any(
			before.get(k) != self.get(k)
			for k in (
				"identity_key",
				"company",
				"hospital",
				"organization_unit",
				"master_data_domain",
				"target_doctype",
				"target_name",
			)
		):
			raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_save(self):
		if not reference_write_allowed():
			raise_ione_error("OPERATION_NOT_ALLOWED")
		if self.is_new():
			self.revision = 1
		else:
			if self.flags.locked_revision is None:
				raise_ione_error("OPERATION_NOT_ALLOWED")
			self.revision = int(self.flags.locked_revision) + 1

	def as_public_dict(self):
		return {
			k: (str(self.get(k)) if self.get(k) else None) if k in {"valid_from", "valid_to"} else self.get(k)
			for k in (
				"name",
				"company",
				"hospital",
				"organization_unit",
				"master_data_domain",
				"target_doctype",
				"target_name",
				"enabled",
				"valid_from",
				"valid_to",
				"revision",
			)
		}

	def on_trash(self):
		raise_ione_error("OPERATION_NOT_ALLOWED")

	def before_rename(self, *args, **kwargs):
		raise_ione_error("OPERATION_NOT_ALLOWED")
