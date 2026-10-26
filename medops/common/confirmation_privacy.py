from __future__ import annotations

from medops.common.private_values import PrivateText

REDACTED_CONFIRMATION = "[redacted human confirmation]"


class ConfirmationPayload(str):
	"""An RPC carrier whose serialized/logged value never contains the request body."""

	_value: object

	def __new__(cls, value: object):
		instance = super().__new__(cls, REDACTED_CONFIRMATION)
		instance._value = value
		return instance

	def unwrap(self) -> object:
		return self._value

	def __deepcopy__(self, memo):
		# Frappe copies form_dict for log metadata. A diagnostic copy must not
		# retain a second reference to the live proof-bearing body.
		del memo
		return REDACTED_CONFIRMATION

	def __sentry_repr__(self) -> str:
		return REDACTED_CONFIRMATION


class ConfirmationProof(PrivateText):
	"""Preserve proof bytes for validation while hiding ordinary traceback repr."""

	redaction = REDACTED_CONFIRMATION
