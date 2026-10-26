from __future__ import annotations

from frappe.utils import get_datetime, now_datetime

from medops.common.domain_service import DomainServiceDefinition
from medops.common.integration_outbox import (
	OUTBOX_DEFINITION,
	RETIREMENT_REASONS,
	RETIREMENT_ROLES,
	publication_values,
	retirement_values,
)
from medops.services.errors import raise_ione_error, require_roles
from medops.services.transactional_message import (
	ClaimOutboxService,
	CompleteOutboxService,
	FailOutboxService,
	PublishOutboxService,
	RetireOutboxService,
	TransactionalMessageDocument,
	message_write_operation,
)


class IntegrationOutboxDocument(TransactionalMessageDocument):
	message_box_definition = OUTBOX_DEFINITION

	def before_insert(self):
		self.require_publication()
		super().before_insert()

	def require_publication(self):
		expected = publication_values()
		if expected is None:
			raise_ione_error("OPERATION_NOT_ALLOWED")
		for key in (
			"event_id",
			"event_type",
			"source",
			"destination",
			"payload_hash",
			"reference_doctype",
			"reference_name",
		):
			if self.get(key) != expected.get(key):
				raise_ione_error("CONFIGURATION_INVALID")

	def db_insert(self, *args, **kwargs):
		self.require_publication()
		super().db_insert(*args, **kwargs)

	def db_update(self, *args, **kwargs):
		if message_write_operation(self) == "retire":
			previous = self.get_doc_before_save()
			expected = require_retirement(previous)
			if self.status != "Dead Letter" or self.last_error_code != expected.reason_code:
				raise_ione_error("CONFIGURATION_INVALID")
		super().db_update(*args, **kwargs)


def require_retirement(document):
	require_roles(RETIREMENT_ROLES)
	expected = retirement_values()
	if expected is None or document is None:
		raise_ione_error("OPERATION_NOT_ALLOWED")
	if (
		document.name != expected.message
		or document.payload_hash != expected.payload_hash
		or int(document.attempt_count) != expected.attempt
		or document.status != expected.status
	):
		raise_ione_error("CONFLICT")
	if expected.reason_code == "MEDOPS-RELEASE-LEASE-EXHAUSTED" and (
		document.status != "Processing"
		or int(document.attempt_count) != OUTBOX_DEFINITION.max_attempts
		or get_datetime(document.lease_expires_at) > now_datetime()
	):
		raise_ione_error("INVALID_STATE_TRANSITION")
	return expected


class PublishIntegrationOutbox(PublishOutboxService):
	message_box_definition = OUTBOX_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_integration.outbox.publish",
		version=1,
		kind="command",
		required_roles=OUTBOX_DEFINITION.required_roles,
	)


class ClaimIntegrationOutbox(ClaimOutboxService):
	message_box_definition = OUTBOX_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_integration.outbox.claim",
		version=1,
		kind="command",
		required_roles=OUTBOX_DEFINITION.required_roles,
	)


class CompleteIntegrationOutbox(CompleteOutboxService):
	message_box_definition = OUTBOX_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_integration.outbox.complete",
		version=1,
		kind="command",
		required_roles=OUTBOX_DEFINITION.required_roles,
	)


class FailIntegrationOutbox(FailOutboxService):
	message_box_definition = OUTBOX_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_integration.outbox.fail",
		version=1,
		kind="command",
		required_roles=OUTBOX_DEFINITION.required_roles,
	)


class RetireIntegrationOutbox(RetireOutboxService):
	message_box_definition = OUTBOX_DEFINITION
	definition = DomainServiceDefinition(
		name="hrp_integration.outbox.retire",
		version=1,
		kind="command",
		required_roles=OUTBOX_DEFINITION.required_roles,
	)

	def authorize(self, command):
		super().authorize(command)
		require_roles(RETIREMENT_ROLES)

	def authorize_retirement(self, document, command):
		expected = require_retirement(document)
		if expected.reason_code != command.reason_code:
			raise_ione_error("CONFLICT")
		return RETIREMENT_REASONS[expected.reason_code]
