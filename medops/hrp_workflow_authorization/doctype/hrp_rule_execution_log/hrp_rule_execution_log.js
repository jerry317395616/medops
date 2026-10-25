frappe.ui.form.on("HRP Rule Execution Log", {
	refresh(frm) {
		frm.disable_save();
		frm.set_intro(__("这是历史验证证据。当前业务动作必须重新执行规则校验。"));
	},
});
