from __future__ import annotations

REDACTED_VALUE = "[redacted private value]"


class PrivateBytes(bytes):
	"""Keep the byte protocol intact without exposing buffers in diagnostics."""

	def __repr__(self) -> str:
		return repr(REDACTED_VALUE.encode())

	def __sentry_repr__(self) -> str:
		return REDACTED_VALUE


class PrivateText(str):
	"""Wire-compatible text with an explicit private diagnostic representation."""

	redaction = REDACTED_VALUE

	def __repr__(self) -> str:
		return repr(self.redaction)

	def __sentry_repr__(self) -> str:
		# The pinned SDK checks this before traversing string/byte values.
		return self.redaction

	def encode(self, encoding: str = "utf-8", errors: str = "strict") -> PrivateBytes:
		return PrivateBytes(super().encode(encoding, errors))


def private_json_strings(value: object) -> object:
	"""Protect normalized JSON values; retain public keys and scalar wire types."""
	if isinstance(value, str):
		return PrivateText(value)
	if isinstance(value, list):
		return [private_json_strings(item) for item in value]
	if isinstance(value, dict):
		return {key: private_json_strings(item) for key, item in value.items()}
	return value
