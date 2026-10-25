from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from medops.common.domain_service import canonical_json, fingerprint_json
from medops.common.organization import (
	normalize_boolean,
	normalize_code,
	normalize_nonnegative_integer,
	normalize_optional_date,
	normalize_optional_text,
	normalize_required_date,
	normalize_required_text,
	validate_date_range,
)
from medops.common.segregation import SEGREGATION_ACTIONS

MAX_TREE_NODES = 64
MAX_TREE_DEPTH = 5
MAX_RULES = 64
MAX_FIELD_PATHS = 128
MAX_CHILD_ROWS = 2000
MAX_COMPARISONS = 100000
MAX_RULE_JSON_BYTES = 16384
RULE_ACTIONS = ("validate", *SEGREGATION_ACTIONS)
FIELD_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
OPERATORS = frozenset(
	{"required", "empty", "eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "length_lte"}
)
TEXT_TYPES = frozenset({"Data", "Select", "Link", "Small Text"})
NUMBER_TYPES = frozenset({"Int", "Float", "Currency", "Percent", "Duration"})
POLICY_FIELDS = frozenset(
	{
		"code",
		"display_name",
		"target_doctype",
		"target_action",
		"company",
		"hospital",
		"organization_unit",
		"include_descendants",
		"enabled",
		"priority",
		"valid_from",
		"valid_to",
		"severity",
		"when_json",
		"assertion_json",
		"remarks",
	}
)


class BusinessRuleError(ValueError):
	"""A bounded declarative rule or its source contract is invalid."""


def _field(value: object) -> str:
	if (
		not isinstance(value, str)
		or not FIELD_PATTERN.fullmatch(value)
		or any(part in value for part in ("password", "secret", "token", "api_key", "private_key"))
	):
		raise BusinessRuleError("invalid or sensitive field")
	return value


def _json(value: object) -> Any:
	if isinstance(value, str):
		if len(value.encode("utf-8")) > MAX_RULE_JSON_BYTES:
			raise BusinessRuleError("rule JSON exceeds limit")
		try:
			value = json.loads(value)
		except ValueError as exc:
			raise BusinessRuleError("invalid rule JSON") from exc
	if len(canonical_json(value).encode("utf-8")) > MAX_RULE_JSON_BYTES:
		raise BusinessRuleError("rule JSON exceeds limit")
	return value


def normalize_tree(value: object, *, optional: bool = False) -> dict[str, Any] | None:
	if optional and value in (None, "", "null"):
		return None
	loaded = _json(value)
	nodes = [0]

	def walk(node: object, depth: int, in_table: bool) -> dict[str, Any]:
		nodes[0] += 1
		if depth > MAX_TREE_DEPTH or nodes[0] > MAX_TREE_NODES or not isinstance(node, dict):
			raise BusinessRuleError("rule tree exceeds shape or complexity limits")
		if len(node) == 1 and next(iter(node)) in {"all", "any"}:
			key = next(iter(node))
			children = node[key]
			if not isinstance(children, list) or not 1 <= len(children) <= 16:
				raise BusinessRuleError("boolean groups require one to sixteen conditions")
			return {key: [walk(child, depth + 1, in_table) for child in children]}
		if set(node) == {"table", "quantifier", "where"}:
			if in_table or node["quantifier"] not in ("all", "any"):
				raise BusinessRuleError("nested tables or unknown quantifiers are not supported")
			return {
				"table": _field(node["table"]),
				"quantifier": node["quantifier"],
				"where": walk(node["where"], depth + 1, True),
			}
		op = node.get("op")
		if not isinstance(op, str) or op not in OPERATORS:
			raise BusinessRuleError("unknown operator")
		field = _field(node.get("field"))
		if op in {"required", "empty"}:
			if set(node) != {"field", "op"}:
				raise BusinessRuleError("presence operators have no operand")
			return {"field": field, "op": op}
		if "other_field" in node:
			if set(node) != {"field", "op", "other_field"} or op not in {
				"eq",
				"ne",
				"gt",
				"gte",
				"lt",
				"lte",
			}:
				raise BusinessRuleError("invalid field comparison")
			return {"field": field, "op": op, "other_field": _field(node["other_field"])}
		if set(node) != {"field", "op", "value"}:
			raise BusinessRuleError("invalid comparison shape")
		operand = node["value"]
		if op in {"in", "not_in"}:
			if (
				not isinstance(operand, list)
				or not 1 <= len(operand) <= 32
				or any(isinstance(item, (list, dict)) or item is None for item in operand)
			):
				raise BusinessRuleError("set comparison requires bounded scalar operands")
			if len({canonical_json(v) for v in operand}) != len(operand):
				raise BusinessRuleError("duplicate set operand")
			operand = sorted(operand, key=canonical_json)
		elif isinstance(operand, (list, dict)) or operand is None:
			raise BusinessRuleError("comparison requires a scalar")
		if any(
			isinstance(v, str) and len(v) > 500 for v in (operand if isinstance(operand, list) else [operand])
		):
			raise BusinessRuleError("operand is too large")
		return {"field": field, "op": op, "value": operand}

	return walk(loaded, 0, False)


def field_paths(tree: dict[str, Any] | None, prefix: str = "") -> set[str]:
	if tree is None:
		return set()
	if "table" in tree:
		return {tree["table"], *field_paths(tree["where"], tree["table"] + ".")}
	for group in ("all", "any"):
		if group in tree:
			return set().union(*(field_paths(child, prefix) for child in tree[group]))
	return {prefix + tree["field"], *([prefix + tree["other_field"]] if "other_field" in tree else [])}


def scalar(value: object, contract: dict[str, Any]) -> Any:
	kind = contract["kind"]
	if kind == "number":
		if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
			raise BusinessRuleError("numeric operand expected")
		try:
			number = Decimal(str(value))
		except InvalidOperation as exc:
			raise BusinessRuleError("invalid number") from exc
		exponent = number.as_tuple().exponent
		if (
			not number.is_finite()
			or abs(number) >= Decimal("1e24")
			or not isinstance(exponent, int)
			or exponent < -12
		):
			raise BusinessRuleError("number outside supported range")
		return number
	if kind == "boolean":
		if not isinstance(value, (int, bool)) or value not in (0, 1):
			raise BusinessRuleError("boolean operand expected")
		return bool(value)
	if kind in {"date", "datetime"}:
		try:
			if kind == "date":
				if isinstance(value, datetime):
					raise ValueError("datetime is not a date")
				return value if isinstance(value, date) else date.fromisoformat(str(value))
			stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
			if stamp.tzinfo:
				raise ValueError("datetimes must follow site-timezone conventions")
			return stamp
		except (ValueError, TypeError) as exc:
			raise BusinessRuleError("invalid date operand") from exc
	if kind == "text":
		if not isinstance(value, str) or len(value) > 500:
			raise BusinessRuleError("bounded text expected")
		if contract["fieldtype"] == "Select" and value and value not in contract["options"]:
			raise BusinessRuleError("select operand is outside declared options")
		return value
	raise BusinessRuleError("unsupported scalar type")


def validate_tree_contract(
	tree: dict[str, Any] | None, contracts: dict[str, dict[str, Any]], prefix: str = ""
) -> None:
	if tree is None:
		return
	if "table" in tree:
		if contracts[tree["table"]]["kind"] != "table":
			raise BusinessRuleError("table field required")
		validate_tree_contract(tree["where"], contracts, tree["table"] + ".")
		return
	for group in ("all", "any"):
		if group in tree:
			for child in tree[group]:
				validate_tree_contract(child, contracts, prefix)
			return
	contract = contracts[prefix + tree["field"]]
	op = tree["op"]
	if contract["kind"] == "table":
		raise BusinessRuleError("tables require a quantifier")
	if op in {"gt", "gte", "lt", "lte"} and contract["kind"] not in {"number", "date", "datetime"}:
		raise BusinessRuleError("ordering requires numbers or dates")
	if op == "length_lte":
		if (
			contract["kind"] != "text"
			or isinstance(tree["value"], bool)
			or not isinstance(tree["value"], int)
			or not 1 <= tree["value"] <= 500
		):
			raise BusinessRuleError("invalid maximum length")
		return
	if op in {"required", "empty"}:
		return
	if "other_field" in tree:
		other = contracts[prefix + tree["other_field"]]
		if other["kind"] != contract["kind"] or (
			(contract["fieldtype"] == "Currency" or other["fieldtype"] == "Currency")
			and (other["fieldtype"] != contract["fieldtype"] or other["options"] != contract["options"])
		):
			raise BusinessRuleError("incompatible field comparison or currency units")
	else:
		for value in tree["value"] if op in {"in", "not_in"} else [tree["value"]]:
			scalar(value, contract)


@dataclass
class EvaluationBudget:
	remaining: int = MAX_COMPARISONS

	def consume(self) -> None:
		self.remaining -= 1
		if self.remaining < 0:
			raise BusinessRuleError("evaluation exceeds operation budget")


def evaluate_tree(
	tree: dict[str, Any] | None,
	values: dict[str, Any],
	contracts: dict[str, dict[str, Any]],
	*,
	budget: EvaluationBudget,
	prefix: str = "",
) -> bool:
	if tree is None:
		return True
	budget.consume()
	if "table" in tree:
		rows = values.get(tree["table"]) or []
		if (
			not isinstance(rows, list)
			or len(rows) > MAX_CHILD_ROWS
			or any(not isinstance(row, dict) for row in rows)
		):
			raise BusinessRuleError("invalid or excessive child rows")
		# Both quantifiers require a nonempty table; an empty purchase request must not pass all().
		results = [
			evaluate_tree(tree["where"], row, contracts, budget=budget, prefix=tree["table"] + ".")
			for row in rows
		]
		return bool(results) and (all(results) if tree["quantifier"] == "all" else any(results))
	for group in ("all", "any"):
		if group in tree:
			results = [
				evaluate_tree(child, values, contracts, budget=budget, prefix=prefix) for child in tree[group]
			]
			return all(results) if group == "all" else any(results)
	raw = values.get(tree["field"])
	present = raw is not None and (not isinstance(raw, str) or bool(raw.strip()))
	op = tree["op"]
	if op in {"required", "empty"}:
		return present if op == "required" else not present
	if not present:
		return False
	contract = contracts[prefix + tree["field"]]
	left = scalar(raw, contract)
	if op == "length_lte":
		return len(str(left)) <= tree["value"]
	if "other_field" in tree:
		other = values.get(tree["other_field"])
		if other is None or other == "":
			return False
		right = scalar(other, contracts[prefix + tree["other_field"]])
	elif op in {"in", "not_in"}:
		right = [scalar(value, contract) for value in tree["value"]]
	else:
		right = scalar(tree["value"], contract)
	if op == "eq":
		return left == right
	if op == "ne":
		return left != right
	if op == "in":
		return left in right
	if op == "not_in":
		return left not in right
	if op == "gt":
		return left > right
	if op == "gte":
		return left >= right
	if op == "lt":
		return left < right
	if op == "lte":
		return left <= right
	raise BusinessRuleError("unknown comparison")


def rule_payload(value: object) -> dict[str, Any]:
	if not isinstance(value, dict) or set(value) - POLICY_FIELDS:
		raise BusinessRuleError("unsupported policy fields")
	p = dict(value)
	try:
		p["code"] = normalize_code(p.get("code"), label="code")
		for key in ("display_name", "target_doctype", "company", "hospital"):
			p[key] = normalize_required_text(p.get(key), label=key, maximum=140)
		p["organization_unit"] = normalize_optional_text(
			p.get("organization_unit"), label="organization_unit", maximum=140
		)
		for key, default in (("include_descendants", False), ("enabled", True)):
			p[key] = int(normalize_boolean(p.get(key, default), label=key))
		if p["include_descendants"] and not p["organization_unit"]:
			raise BusinessRuleError("descendants require a scope")
		p["priority"] = normalize_nonnegative_integer(p.get("priority", 0), label="priority")
		if p["priority"] > 1000:
			raise BusinessRuleError("priority exceeds limit")
		p["valid_from"] = normalize_required_date(p.get("valid_from"), label="valid_from")
		p["valid_to"] = normalize_optional_date(p.get("valid_to"), label="valid_to")
		validate_date_range(p["valid_from"], p["valid_to"])
		p["remarks"] = normalize_optional_text(p.get("remarks"), label="remarks", maximum=500)
	except ValueError as exc:
		raise BusinessRuleError(str(exc)) from exc
	if p.get("target_action") not in RULE_ACTIONS or p.get("severity") not in ("Block", "Warn"):
		raise BusinessRuleError("unsupported action or severity")
	p["when_json"] = canonical_json(normalize_tree(p.get("when_json"), optional=True))
	p["assertion_json"] = canonical_json(normalize_tree(p.get("assertion_json")))
	return p


def policy_digest(payload: dict[str, Any], contracts: dict[str, dict[str, Any]]) -> str:
	return fingerprint_json({"policy": payload, "field_contracts": contracts})
