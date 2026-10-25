from __future__ import annotations

import frappe

OWNED_WORKSPACES = (
	("hrp_foundation", "hrp"),
	("hrp_organization", "hrp_organization"),
	("hrp_master_data", "hrp_master_data"),
	("hrp_workflow_authorization", "hrp_authorization"),
)


def sync_owned_workspaces() -> dict[str, object]:
	"""Force-sync app-owned standard workspaces even when a site copy is newer."""
	for module, workspace in OWNED_WORKSPACES:
		frappe.reload_doc(module, "workspace", workspace, force=True)
	return {"schema_version": 1, "workspaces": [name for _, name in OWNED_WORKSPACES]}


__all__ = ["OWNED_WORKSPACES", "sync_owned_workspaces"]
