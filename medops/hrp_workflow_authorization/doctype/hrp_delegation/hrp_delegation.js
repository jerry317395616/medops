frappe.ui.form.on("HRP Delegation", {
	refresh(frm) {
		const isAdministrator = ["System Manager", "HRP System Manager"].some((role) =>
			frappe.user_roles.includes(role),
		);
		if (
			!["Active", "Scheduled"].includes(frm.doc.status) ||
			(!isAdministrator && frm.doc.from_user !== frappe.session.user)
		) {
			return;
		}
		frm.add_custom_button(__("撤销委托"), () => {
			frappe.prompt(
				[{ fieldname: "reason", fieldtype: "Small Text", label: __("撤销原因"), reqd: 1 }],
				async (values) => {
					await frappe.call({
						method: "medops.api.v1.core.delegation.revoke",
						args: { delegation: frm.doc.name, reason: values.reason },
						headers: { "Idempotency-Key": `delegation-revoke-${frm.doc.name}-${frappe.utils.get_random(8)}` },
						freeze: true,
						freeze_message: __("正在撤销审批委托"),
					});
					await frm.reload_doc();
				},
				__("撤销审批委托"),
				__("确认撤销"),
			);
		});
	},
});
