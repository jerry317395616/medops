"""Explicit grants for synthetic acceptance actors, never called by installation."""

from __future__ import annotations

import frappe


def grant_test_scope(*, code: str, company: str, hospital: str, users: tuple[str, ...]) -> None:
	assert frappe.session.user == "Administrator"
	assert code.startswith("COD") and all(user.endswith("@example.com") for user in users)
	if frappe.db.exists("HRP Access Scope", code):
		return
	frappe.get_doc(
		{
			"doctype": "HRP Access Scope",
			"code": code,
			"display_name": "受控测试数据范围",
			"company": company,
			"hospital": hospital,
			"enabled": 1,
			"members": [
				{
					"user": user,
					"enabled": 1,
					"allow_read": 1,
					"allow_create": 1,
					"allow_write": 1,
					"allow_submit": 1,
					"allow_export": 1,
				}
				for user in users
			],
		}
	).insert(ignore_permissions=True)


def share_test_records(doctype: str, names: tuple[str, ...], users: tuple[str, ...]) -> None:
	assert frappe.session.user == "Administrator"
	assert all(user.endswith("@example.com") for user in users)
	for name in names:
		for user in users:
			frappe.share.add(doctype, name, user=user, read=1, write=0, share=0, notify=0)
