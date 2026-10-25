frappe.pages["hrp-rule-console"].on_page_load = (wrapper) => {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("业务规则验证"), single_column: true });
	const controls = $("<div class='row'>").appendTo(page.body);
	const values = {};
	let snapshot = null;
	for (const df of [
		{ fieldname: "doctype", label: __("单据类型"), fieldtype: "Link", options: "DocType" },
		{ fieldname: "name", label: __("单据编号"), fieldtype: "Data" },
		{
			fieldname: "action",
			label: __("受控动作"),
			fieldtype: "Select",
			options: "validate\nsubmit\nreview\napprove\ncancel\ncreate\namend\npay\npost\nreconcile\nrelease",
			default: "validate",
		},
	]) {
		const parent = $("<div class='col-md-4'>").appendTo(controls);
		values[df.fieldname] = frappe.ui.form.make_control({
			parent,
			df: { ...df, reqd: 1, onchange: () => (snapshot = null) },
			render_input: true,
		});
	}
	const body = $("<div class='mt-3'>").appendTo(page.body);
	const show = (result) => {
		body.empty();
		$("<p>")
			.text(`${__("本次试算")}: ${__(result.outcome)} · ${__("规则数量")}: ${result.rules_evaluated}`)
			.appendTo(body);
		$("<p class='text-muted'>")
			.text(__("试算和历史记录不会批准、付款或修改业务单据。业务操作仍会实时校验。"))
			.appendTo(body);
		for (const row of result.rules)
			$("<p>")
				.text(`${row.rule} · v${row.revision} · ${__(row.status)}`)
				.appendTo(body);
	};
	page.set_primary_action(__("实时试算"), async () => {
		snapshot = null;
		const args = Object.fromEntries(Object.entries(values).map(([k, control]) => [k, control.get_value()]));
		const { message } = await frappe.call({
			method: "medops.api.v1.core.rules.evaluate",
			args,
			freeze: true,
		});
		snapshot = {
			...args,
			expected_source_modified: message.source_modified,
			expected_decision_digest: message.decision_digest,
		};
		show(message);
	});
	page.set_secondary_action(__("留存本次验证"), async () => {
		if (!snapshot) return frappe.msgprint(__("请先完成实时试算。"));
		const { message } = await frappe.call({
			method: "medops.api.v1.core.rules.record",
			args: snapshot,
			headers: { "Idempotency-Key": `rule-evidence-${frappe.utils.get_random(16)}` },
			freeze: true,
		});
		$("<p>")
			.text(`${__("已留存历史记录")}: ${message.log}`)
			.appendTo(body);
		snapshot = null;
	});
	page.add_menu_item(__("查看可见记录"), async () => {
		snapshot = null;
		const args = { limit: 20 };
		const doctype = values.doctype.get_value();
		const name = values.name.get_value();
		if (doctype && name) Object.assign(args, { doctype, name });
		const { message } = await frappe.call({ method: "medops.api.v1.core.rules.logs", args });
		body.empty();
		$("<p>").text(__("以下是历史验证结果；当前业务仍须重新验证。")).appendTo(body);
		for (const item of message.items)
			$("<p>")
				.text(
					`${item.name} · ${item.reference_doctype} / ${item.reference_name} · ${__(item.recorded_outcome)}`,
				)
				.appendTo(body);
	});
};
