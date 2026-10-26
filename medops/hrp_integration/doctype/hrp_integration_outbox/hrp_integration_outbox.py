from __future__ import annotations

from typing import TYPE_CHECKING

from medops.hrp_integration.services.outbox import IntegrationOutboxDocument


class HRPIntegrationOutbox(IntegrationOutboxDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.
	if TYPE_CHECKING:
		from frappe.types import DF

		attempt_count: DF.Int
		available_at: DF.Datetime
		causation_id: DF.Data | None
		claim_owner: DF.Data | None
		completed_at: DF.Datetime | None
		correlation_id: DF.Data
		destination: DF.Data
		event_id: DF.Data
		event_type: DF.Data
		last_error_code: DF.Data | None
		last_error_message: DF.SmallText | None
		lease_expires_at: DF.Datetime | None
		occurred_at: DF.Datetime
		payload_hash: DF.Data | None
		payload_json: DF.Code
		payload_schema_version: DF.Int
		processing_token_hash: DF.Data | None
		reference_doctype: DF.Link | None
		reference_name: DF.DynamicLink | None
		result_hash: DF.Data | None
		result_json: DF.Code | None
		source: DF.Data
		status: str
	# end: auto-generated types
