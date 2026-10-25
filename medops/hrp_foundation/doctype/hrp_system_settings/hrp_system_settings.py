from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe.model.document import Document

from medops.common.system_settings import (
	LOCKED_RELEASE_CHANNEL,
	SystemSettingsContractError,
	SystemSettingsState,
	build_system_settings_state,
	build_system_settings_update,
	normalize_boolean,
	normalize_positive_integer,
)
from medops.services.errors import raise_ione_error


class HRPSystemSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	if TYPE_CHECKING:
		from frappe.types import DF

		configuration_version: DF.Int
		default_company: DF.Link | None
		default_hospital: DF.Link | None
		enabled: DF.Check
		integration_timeout_seconds: DF.Int
		release_channel: DF.Data
		remarks: DF.SmallText | None
		require_human_confirmation_for_ai: DF.Check
		strict_data_scope: DF.Check
	# end: auto-generated types

	def validate(self) -> None:
		repairing = bool(getattr(self.flags, "system_settings_repair", False))
		if repairing:
			self.release_channel = LOCKED_RELEASE_CHANNEL
			self.strict_data_scope = 1
			self.require_human_confirmation_for_ai = 1
		else:
			try:
				strict_data_scope = normalize_boolean(
					self.strict_data_scope,
					label="strict_data_scope",
				)
				require_human_confirmation = normalize_boolean(
					self.require_human_confirmation_for_ai,
					label="require_human_confirmation_for_ai",
				)
			except SystemSettingsContractError:
				raise_ione_error("OPERATION_NOT_ALLOWED")
			if self.release_channel != LOCKED_RELEASE_CHANNEL or not (
				strict_data_scope and require_human_confirmation
			):
				raise_ione_error("OPERATION_NOT_ALLOWED")

		try:
			update = build_system_settings_update(
				enabled=self.enabled,
				default_company=self.default_company,
				default_hospital=self.default_hospital,
				integration_timeout_seconds=self.integration_timeout_seconds,
				remarks=self.remarks,
				expected_version=self.configuration_version or 1,
			)
		except SystemSettingsContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)

		if update.default_company and not frappe.db.exists("Company", update.default_company):
			raise_ione_error("RESOURCE_NOT_FOUND")
		if update.default_hospital:
			hospital_company = frappe.db.get_value(
				"HRP Hospital",
				update.default_hospital,
				"company",
			)
			if not hospital_company:
				raise_ione_error("RESOURCE_NOT_FOUND")
			if hospital_company != update.default_company:
				raise_ione_error("CONFLICT")
		self.enabled = int(update.enabled)
		self.default_company = update.default_company
		self.default_hospital = update.default_hospital
		self.integration_timeout_seconds = update.integration_timeout_seconds
		self.remarks = update.remarks

	def before_save(self) -> None:
		locked_version = getattr(self.flags, "locked_configuration_version", None)
		if locked_version is None:
			locked_version, persisted = self.lock_configuration(
				self.configuration_version or 1,
				repair_invalid=bool(getattr(self.flags, "system_settings_repair", False)),
			)
		else:
			persisted = True
		self.configuration_version = locked_version + 1 if persisted else 1

	@staticmethod
	def lock_configuration(
		expected_version: object,
		*,
		repair_invalid: bool = False,
	) -> tuple[int, bool]:
		try:
			expected = normalize_positive_integer(
				expected_version,
				label="expected_version",
			)
		except SystemSettingsContractError as exc:
			raise_ione_error("INVALID_REQUEST", cause=exc)

		rows = frappe.db.sql(
			"""
			SELECT value
			FROM `tabSingles`
			WHERE doctype = %s AND field = %s
			FOR UPDATE
			""",
			("HRP System Settings", "configuration_version"),
			as_dict=True,
		)
		persisted = bool(rows)
		try:
			current = (
				normalize_positive_integer(
					rows[0].value,
					label="configuration_version",
				)
				if persisted
				else 1
			)
		except SystemSettingsContractError as exc:
			if repair_invalid:
				return 1, False
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
		if expected != current:
			raise_ione_error("CONFLICT")
		return current, persisted

	def as_contract_state(self) -> SystemSettingsState:
		try:
			return build_system_settings_state(
				enabled=self.enabled,
				default_company=self.default_company,
				default_hospital=self.default_hospital,
				integration_timeout_seconds=self.integration_timeout_seconds,
				remarks=self.remarks,
				configuration_version=self.configuration_version or 1,
				release_channel=self.release_channel,
				strict_data_scope=self.strict_data_scope,
				require_human_confirmation_for_ai=self.require_human_confirmation_for_ai,
			)
		except SystemSettingsContractError as exc:
			raise_ione_error("CONFIGURATION_INVALID", cause=exc)
