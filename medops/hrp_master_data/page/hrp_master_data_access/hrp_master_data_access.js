frappe.pages["hrp-master-data-access"].on_page_load = (wrapper) => {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("主数据受控查询"), single_column: true });
	const controls = $("<div class='row'>").appendTo(page.body);
	const fields = {};
	let offset = 0;
	const entities = { 变更申请: "requests", 外部编码映射: "mappings", 质量规则: "rules", 质量问题: "issues" };
	for (const df of [
		{
			fieldname: "entity",
			label: __("记录类型"),
			fieldtype: "Select",
			options: Object.keys(entities).join("\n"),
			default: "变更申请",
		},
		{ fieldname: "company", label: __("法人"), fieldtype: "Link", options: "Company" },
		{ fieldname: "hospital", label: __("医院"), fieldtype: "Link", options: "HRP Hospital" },
		{
			fieldname: "organization_unit",
			label: __("科室或组织"),
			fieldtype: "Link",
			options: "HRP Organization Unit",
		},
		{ fieldname: "from_date", label: __("更新日期起"), fieldtype: "Date" },
		{ fieldname: "to_date", label: __("更新日期止"), fieldtype: "Date" },
	]) {
		fields[df.fieldname] = frappe.ui.form.make_control({
			parent: $("<div class='col-md-4'>").appendTo(controls),
			df: { ...df, onchange: () => (offset = 0) },
			render_input: true,
		});
	}
	$("<p class='text-muted'>")
		.text(__("仅显示当前有权读取的记录。记录较多时，请缩小医院、科室或日期范围。"))
		.appendTo(page.body);
	const body = $("<div>").appendTo(page.body);
	const more = $("<button class='btn btn-default mt-3'>").text(__("下一页")).appendTo(page.body).hide();
	const load = async () => {
		const args = Object.fromEntries(
			Object.entries(fields).map(([key, control]) => [key, control.get_value()]),
		);
		args.entity = entities[args.entity];
		args.offset = offset;
		args.limit = 20;
		more.hide();
		const { message } = await frappe.call({
			method: "medops.api.v1.master_data_access.list_records",
			type: "GET",
			args,
			freeze: true,
		});
		body.empty();
		if (!message.items.length) $("<p>").text(__("当前条件下没有可见记录。")).appendTo(body);
		for (const row of message.items) {
			const item = $("<div class='border-bottom py-3'>").appendTo(body);
			$("<button class='btn btn-link p-0'>")
				.text(row.title)
				.on("click", () => frappe.set_route("Form", row.doctype, row.name))
				.appendTo(item);
			$("<div class='text-muted'>")
				.text(`${row.name} · ${__(row.status)} · ${row.hospital} · ${row.modified}`)
				.appendTo(item);
		}
		if (message.next_offset !== null)
			more
				.show()
				.off("click")
				.on("click", () => {
					offset = message.next_offset;
					load();
				});
	};
	page.set_primary_action(__("查询"), () => {
		offset = 0;
		load();
	});
};
