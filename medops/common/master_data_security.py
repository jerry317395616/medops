from __future__ import annotations

import json

from medops.common.domain_service import canonical_json
from medops.common.master_data import MASTER_DATA_TARGET_POLICIES


def evaluation_fields(value: object, target_doctype: str) -> list[str]:
	policy = MASTER_DATA_TARGET_POLICIES.get(target_doctype)
	if policy is None:
		raise ValueError("unregistered master-data target")
	if isinstance(value, str):
		if len(value.encode("utf-8")) > 4096:
			raise ValueError("evaluation access contract exceeds limit")
		value = json.loads(value)
	if (
		not isinstance(value, list)
		or not 1 <= len(value) <= 64
		or any(not isinstance(field, str) or field not in policy.fields_by_name for field in value)
	):
		raise ValueError("reviewed evaluation fields required")
	if len(set(value)) != len(value):
		raise ValueError("duplicate evaluation field")
	return sorted(value)


def merge_evaluation_fields(
	previous: object, target_doctype: str, current_field: str, *, legacy: bool = False
) -> str:
	policy = MASTER_DATA_TARGET_POLICIES.get(target_doctype)
	if policy is None or current_field not in policy.fields_by_name:
		raise ValueError("unregistered evaluation field")
	fields = (
		evaluation_fields(previous, target_doctype)
		if previous
		else list(policy.fields_by_name)
		if legacy
		else []
	)
	return canonical_json(sorted({*fields, current_field}))
