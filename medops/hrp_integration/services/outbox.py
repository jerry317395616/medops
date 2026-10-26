from __future__ import annotations

from medops.common.domain_service import DomainServiceDefinition
from medops.common.integration_outbox import OUTBOX_DEFINITION, publication_values
from medops.services.errors import raise_ione_error
from medops.services.transactional_message import (
	ClaimOutboxService,
	CompleteOutboxService,
	FailOutboxService,
	PublishOutboxService,
	TransactionalMessageDocument,
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
