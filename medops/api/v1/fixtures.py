from __future__ import annotations

import frappe

from medops.services.errors import require_authenticated_user
from medops.services.fixtures import get_fixture_governance_status as get_status


@frappe.whitelist(methods=["GET"])
def get_fixture_governance_status() -> dict[str, object]:
	require_authenticated_user()
	return get_status()
