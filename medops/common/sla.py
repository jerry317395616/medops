from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Literal

from medops.common.domain_service import canonical_json
from medops.common.organization import (
	OrganizationContractError,
	normalize_nonnegative_integer,
	normalize_positive_integer,
	normalize_reference,
	normalize_required_text,
)

SLA_SCHEMA_VERSION = 1
MAX_SLA_JSON_BYTES = 16 * 1024
MAX_SLA_DURATION_SECONDS = 90 * 24 * 60 * 60
MAX_SLA_ESCALATION_STEPS = 5
MAX_SLA_STATUSES = 32
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
TIME_PATTERN = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$")
FIELD_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ClockBasis = Literal["Elapsed", "Business Hours"]


class SLAContractError(ValueError):
	"""Reject unsafe or ambiguous SLA configuration before database writes."""


def _bounded_list(value: object, *, label: str, maximum: int) -> list[object]:
	if value is None:
		return []
	if isinstance(value, str):
		if len(value.encode("utf-8")) > MAX_SLA_JSON_BYTES:
			raise SLAContractError(f"{label} is too large")
		try:
			value = json.loads(value)
		except json.JSONDecodeError as exc:
			raise SLAContractError(f"{label} must contain valid JSON") from exc
	if not isinstance(value, (list, tuple)) or len(value) > maximum:
		raise SLAContractError(f"{label} must be a bounded list")
	result = list(value)
	try:
		encoded = canonical_json(result)
	except ValueError as exc:
		raise SLAContractError(f"{label} must contain JSON values") from exc
	if len(encoded.encode("utf-8")) > MAX_SLA_JSON_BYTES:
		raise SLAContractError(f"{label} is too large")
	return result


def normalize_sla_field(value: object, *, label: str) -> str:
	if not isinstance(value, str) or FIELD_PATTERN.fullmatch(value) is None:
		raise SLAContractError(f"{label} must be a field name")
	return value


def normalize_sla_duration(value: object, *, label: str, allow_zero: bool = False) -> int:
	try:
		seconds = (
			normalize_nonnegative_integer(value, label=label)
			if allow_zero
			else normalize_positive_integer(value, label=label)
		)
	except OrganizationContractError as exc:
		raise SLAContractError(str(exc)) from exc
	if seconds > MAX_SLA_DURATION_SECONDS:
		raise SLAContractError(f"{label} exceeds the supported duration")
	return seconds


def normalize_sla_statuses(value: object, *, label: str, required: bool = False) -> tuple[str, ...]:
	try:
		statuses = [
			normalize_required_text(item, label=label, maximum=140)
			for item in _bounded_list(value, label=label, maximum=MAX_SLA_STATUSES)
		]
	except OrganizationContractError as exc:
		raise SLAContractError(str(exc)) from exc
	if (required and not statuses) or len(set(statuses)) != len(statuses):
		raise SLAContractError(f"{label} must contain unique statuses")
	return tuple(sorted(statuses))


def validate_sla_status_groups(
	*, start_statuses: object, pause_statuses: object, completion_statuses: object
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
	start = normalize_sla_statuses(start_statuses, label="start_statuses", required=True)
	pause = normalize_sla_statuses(pause_statuses, label="pause_statuses")
	complete = normalize_sla_statuses(completion_statuses, label="completion_statuses", required=True)
	if set(start) & set(pause) or set(start) & set(complete) or set(pause) & set(complete):
		raise SLAContractError("SLA status groups must not overlap")
	return start, pause, complete


def _time_seconds(value: object) -> int:
	if not isinstance(value, str) or TIME_PATTERN.fullmatch(value) is None:
		raise SLAContractError("support times must use HH:MM:SS")
	hour, minute, second = map(int, value.split(":"))
	return hour * 3600 + minute * 60 + second


@dataclass(frozen=True, slots=True)
class SLASupportHours:
	workday: str
	start_time: str
	end_time: str

	def as_erpnext_payload(self) -> dict[str, str]:
		return {"workday": self.workday, "start_time": self.start_time, "end_time": self.end_time}


def build_sla_support_hours(value: object) -> tuple[SLASupportHours, ...]:
	result: list[SLASupportHours] = []
	for item in _bounded_list(value, label="support_hours", maximum=7):
		if not isinstance(item, dict) or set(item) != {"workday", "start_time", "end_time"}:
			raise SLAContractError("support hours contain unsupported fields")
		day, start, end = item["workday"], item["start_time"], item["end_time"]
		if not isinstance(day, str) or day not in WEEKDAYS:
			raise SLAContractError("workday is invalid")
		if _time_seconds(start) >= _time_seconds(end):
			raise SLAContractError("support start must precede end")
		result.append(SLASupportHours(day, str(start), str(end)))
	if len({item.workday for item in result}) != len(result):
		raise SLAContractError("workdays must not repeat")
	return tuple(sorted(result, key=lambda item: WEEKDAYS.index(item.workday)))


def normalize_sla_clock(
	*, clock_basis: object, support_hours: object = None, holiday_list: object = None
) -> tuple[ClockBasis, tuple[SLASupportHours, ...], str | None]:
	if clock_basis not in ("Elapsed", "Business Hours"):
		raise SLAContractError("clock_basis is invalid")
	hours = build_sla_support_hours(support_hours)
	try:
		holiday = normalize_reference(holiday_list, label="holiday_list") if holiday_list else None
	except OrganizationContractError as exc:
		raise SLAContractError(str(exc)) from exc
	if clock_basis == "Elapsed":
		if hours or holiday:
			raise SLAContractError("elapsed clocks cannot silently ignore a calendar")
		return "Elapsed", (), None
	if not hours:
		raise SLAContractError("business-hour clocks require working hours")
	return "Business Hours", hours, holiday


@dataclass(frozen=True, slots=True)
class SLAEscalationStep:
	level: int
	delay_seconds: int
	assigned_to: str

	def as_payload(self) -> dict[str, object]:
		return {"level": self.level, "delay_seconds": self.delay_seconds, "assigned_to": self.assigned_to}


def build_sla_escalation_steps(value: object) -> tuple[SLAEscalationStep, ...]:
	result: list[SLAEscalationStep] = []
	for position, item in enumerate(
		_bounded_list(value, label="escalation_steps", maximum=MAX_SLA_ESCALATION_STEPS), start=1
	):
		if not isinstance(item, dict) or set(item) != {"level", "delay_seconds", "assigned_to"}:
			raise SLAContractError("escalation steps contain unsupported fields")
		try:
			level = normalize_positive_integer(item["level"], label="level")
			user = normalize_reference(item["assigned_to"], label="assigned_to")
		except OrganizationContractError as exc:
			raise SLAContractError(str(exc)) from exc
		delay = normalize_sla_duration(item["delay_seconds"], label="delay_seconds", allow_zero=True)
		if level != position or user == "Guest":
			raise SLAContractError("escalation levels must be contiguous and name a system user")
		if result and delay <= result[-1].delay_seconds:
			raise SLAContractError("escalation delays must strictly increase")
		result.append(SLAEscalationStep(level, delay, user))
	if not result:
		raise SLAContractError("at least one escalation step is required")
	return tuple(result)


POLICY_FIELDS = frozenset(
	{
		"code",
		"display_name",
		"target_doctype",
		"company",
		"hospital",
		"organization_unit",
		"include_descendants",
		"priority",
		"enabled",
		"valid_from",
		"valid_to",
		"status_field",
		"start_field",
		"response_field",
		"start_statuses_json",
		"pause_statuses_json",
		"completion_statuses_json",
		"response_seconds",
		"resolution_seconds",
		"clock_basis",
		"support_hours_json",
		"holiday_list",
		"responsible_user",
		"escalation_steps_json",
		"remarks",
	}
)


def policy_payload(value: object) -> dict[str, Any]:
	"""Normalize the complete declarative policy; never accept executable expressions."""
	from medops.common.organization import (
		normalize_boolean,
		normalize_code,
		normalize_optional_date,
		normalize_required_date,
	)

	if not isinstance(value, dict) or set(value) - POLICY_FIELDS:
		raise SLAContractError("policy contains unsupported fields")
	p = dict(value)
	try:
		p["code"] = normalize_code(p.get("code"), label="code")
		for field in ("display_name", "target_doctype", "company", "hospital", "responsible_user"):
			p[field] = normalize_required_text(p.get(field), label=field, maximum=140)
		if p["responsible_user"] == "Guest":
			raise SLAContractError("responsible_user must be a system user")
		p["organization_unit"] = (
			normalize_reference(p.get("organization_unit"), label="organization_unit")
			if p.get("organization_unit")
			else None
		)
		for field, default in (("enabled", True), ("include_descendants", False)):
			p[field] = int(normalize_boolean(p.get(field, default), label=field))
		if p["include_descendants"] and not p["organization_unit"]:
			raise SLAContractError("descendants require an organization")
		p["priority"] = normalize_nonnegative_integer(p.get("priority", 0), label="priority")
		if p["priority"] > 1000:
			raise SLAContractError("priority exceeds 1000")
		p["valid_from"] = normalize_required_date(p.get("valid_from"), label="valid_from")
		p["valid_to"] = normalize_optional_date(p.get("valid_to"), label="valid_to")
		if p["valid_to"] and p["valid_to"] < p["valid_from"]:
			raise SLAContractError("invalid validity interval")
		p["remarks"] = (
			normalize_required_text(p["remarks"], label="remarks", maximum=500) if p.get("remarks") else None
		)
	except OrganizationContractError as exc:
		raise SLAContractError(str(exc)) from exc
	for field in ("status_field", "start_field"):
		p[field] = normalize_sla_field(p.get(field), label=field)
	p["response_field"] = (
		normalize_sla_field(p["response_field"], label="response_field") if p.get("response_field") else None
	)
	groups = validate_sla_status_groups(
		start_statuses=p.get("start_statuses_json"),
		pause_statuses=p.get("pause_statuses_json"),
		completion_statuses=p.get("completion_statuses_json"),
	)
	for field, group in zip(
		("start_statuses_json", "pause_statuses_json", "completion_statuses_json"), groups, strict=True
	):
		p[field] = canonical_json(group)
	for field in ("response_seconds", "resolution_seconds"):
		p[field] = normalize_sla_duration(p.get(field), label=field)
	if p["response_seconds"] > p["resolution_seconds"]:
		raise SLAContractError("response must precede resolution")
	basis, hours, holidays = normalize_sla_clock(
		clock_basis=p.get("clock_basis"),
		support_hours=p.get("support_hours_json"),
		holiday_list=p.get("holiday_list"),
	)
	p.update(
		clock_basis=basis,
		support_hours_json=canonical_json([h.as_erpnext_payload() for h in hours]),
		holiday_list=holidays,
	)
	p["escalation_steps_json"] = canonical_json(
		[s.as_payload() for s in build_sla_escalation_steps(p.get("escalation_steps_json"))]
	)
	return p


def sla_digest(value: object) -> str:
	return sha256(canonical_json(value).encode("utf-8")).hexdigest()


MAX_CLOCK_DAYS = 3660


@dataclass(frozen=True)
class SLAClock:
	"""Bounded clock over a frozen, site-timezone calendar (Frappe datetime convention)."""

	basis: str
	# Each weekday maps to seconds since midnight, obtained via ERPNext support-day helpers.
	days: dict[str, tuple[float, float]]
	holidays: frozenset[str] = frozenset()
	valid_from: str | None = None
	valid_to: str | None = None

	def _window(self, day: datetime) -> tuple[datetime, datetime] | None:
		key = day.date().isoformat()
		if (self.valid_from and key < self.valid_from) or (self.valid_to and key > self.valid_to):
			raise SLAContractError("frozen holiday calendar does not cover this date")
		hours = self.days.get(WEEKDAYS[day.weekday()])
		if not hours or key in self.holidays:
			return None
		midnight = day.replace(hour=0, minute=0, second=0, microsecond=0)
		return midnight + timedelta(seconds=hours[0]), midnight + timedelta(seconds=hours[1])

	def seconds(self, start: datetime, end: datetime) -> float:
		if end < start:
			raise SLAContractError("clock cannot move backwards")
		if self.basis == "Elapsed":
			return (end - start).total_seconds()
		if (end.date() - start.date()).days > MAX_CLOCK_DAYS:
			raise SLAContractError("clock interval exceeds supported horizon")
		total = 0.0
		day = start.replace(hour=0, minute=0, second=0, microsecond=0)
		while day <= end:
			window = self._window(day)
			if window:
				total += max(0.0, (min(end, window[1]) - max(start, window[0])).total_seconds())
			day += timedelta(days=1)
		return total

	def due(self, start: datetime, seconds: float) -> datetime:
		if seconds < 0 or seconds > 2 * MAX_SLA_DURATION_SECONDS:
			raise SLAContractError("invalid remaining duration")
		if self.basis == "Elapsed" or seconds == 0:
			return start + timedelta(seconds=seconds)
		day = start.replace(hour=0, minute=0, second=0, microsecond=0)
		for _ in range(MAX_CLOCK_DAYS):
			window = self._window(day)
			if window:
				begin = max(start, window[0])
				available = max(0.0, (window[1] - begin).total_seconds())
				if available >= seconds:
					return begin + timedelta(seconds=seconds)
				seconds -= available
			day += timedelta(days=1)
		raise SLAContractError("calendar has insufficient working time within supported horizon")
