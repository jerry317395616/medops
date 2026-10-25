frappe.ui.form.on("HRP SLA Escalation", {
	refresh(frm) {
		frm.disable_save();
		if (frm.is_new() || ["Completed", "Cancelled"].includes(frm.doc.status)) return;
		const act = async (action, reason) => {
			await frappe.call({
				method: "medops.api.v1.core.sla.act",
				args: { case_name: frm.doc.name, action, reason, expected_revision: frm.doc.revision },
				headers: { "Idempotency-Key": `sla-action-${frappe.utils.get_random(16)}` },
				freeze: true,
			});
			await frm.reload_doc();
		};
		if (frm.doc.status === "Open" && !frm.doc.responded_at) {
			frm.add_custom_button(__("确认响应"), () => act("acknowledge"));
		}
		frm.add_custom_button(__("立即检查"), () => act("process"));
		frm.add_custom_button(__("停止计时"), () =>
			frappe.prompt(
				[{ fieldname: "reason", fieldtype: "Small Text", label: __("停止原因"), reqd: 1 }],
				(v) => act("cancel", v.reason),
				__("停止服务时限计时"),
			),
		);
	},
});
