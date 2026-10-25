frappe.listview_settings["HRP Delegation"] = {
	onload(listview) {
		if (
			!["System Manager", "HRP System Manager", "HRP Department Manager"].some((role) =>
				frappe.user_roles.includes(role),
			)
		) {
			return;
		}
		listview.page.add_inner_button(__("新建审批委托"), () => {
			const dialog = new frappe.ui.Dialog({
				title: __("新建审批委托"),
				fields: [
					{
						fieldname: "matrix",
						fieldtype: "Link",
						label: __("审批矩阵"),
						options: "HRP Approval Matrix",
						reqd: 1,
					},
					{
						fieldname: "from_user",
						fieldtype: "Link",
						label: __("委托人"),
						options: "User",
						default: frappe.session.user,
						reqd: 1,
					},
					{ fieldname: "to_user", fieldtype: "Link", label: __("受托人"), options: "User", reqd: 1 },
					{ fieldname: "step_sequence", fieldtype: "Int", label: __("限定审批步骤") },
					{
						fieldname: "valid_from",
						fieldtype: "Date",
						label: __("开始日期"),
						default: frappe.datetime.get_today(),
						reqd: 1,
					},
					{ fieldname: "valid_to", fieldtype: "Date", label: __("结束日期"), reqd: 1 },
					{ fieldname: "reason", fieldtype: "Small Text", label: __("委托原因") },
				],
				primary_action_label: __("创建"),
				async primary_action(values) {
					await frappe.call({
						method: "medops.api.v1.core.delegation.create",
						args: values,
						headers: { "Idempotency-Key": `delegation-create-${frappe.utils.get_random(12)}` },
						freeze: true,
						freeze_message: __("正在创建审批委托"),
					});
					dialog.hide();
					listview.refresh();
				},
			});
			dialog.show();
		});
	},
};
