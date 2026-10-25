frappe.pages["hrp-core-workbench"].on_page_load = (wrapper) => {
	const writable = frappe.user_roles.some((role) =>
		["System Manager", "HRP System Manager", "HRP Data Steward"].includes(role),
	);
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("核心工作台"), single_column: true });
	const controls = $("<div class='row'>").appendTo(page.body),
		fields = {};
	for (const df of [
		{ fieldname: "company", label: __("法人"), fieldtype: "Link", options: "Company" },
		{ fieldname: "hospital", label: __("医院"), fieldtype: "Link", options: "HRP Hospital" },
		{
			fieldname: "organization_unit",
			label: __("科室或组织"),
			fieldtype: "Link",
			options: "HRP Organization Unit",
		},
	])
		fields[df.fieldname] = frappe.ui.form.make_control({
			parent: $("<div class='col-md-4'>").appendTo(controls),
			df,
			render_input: true,
		});
	$("<p class='text-muted'>")
		.text(__("仅显示当前可见事项，每栏最多预览10条；预览数不是全院总量。报表按当前授权范围统计。"))
		.appendTo(page.body);
	const body = $("<div>").appendTo(page.body);
	const context = () =>
		Object.fromEntries(
			Object.entries(fields)
				.map(([key, control]) => [key, control.get_value()])
				.filter(([, value]) => value),
		);
	const load = async () => {
		body.empty();
		const { message } = await frappe.call({
			method: "medops.api.v1.core_workbench.overview",
			type: "GET",
			args: { filters: JSON.stringify(context()) },
			freeze: true,
		});
		for (const [key, label, dt] of [
			["todos", "我的待办", "ToDo"],
			["requests", "待复核申请", "HRP Master Data Request"],
			["issues", "未解决质量问题", "HRP Data Quality Issue"],
			["sla", "服务时限", "HRP SLA Escalation"],
		]) {
			const section = $("<section class='border rounded p-3 my-3'>").appendTo(body),
				data = message.sections[key];
			$("<h4>").text(__(label)).appendTo(section);
			if (!data.available)
				$("<p class='text-muted'>").text(__("当前账号没有此类记录的读取权限。")).appendTo(section);
			else if (!data.items.length)
				$("<p class='text-muted'>").text(__("当前预览范围没有可见事项。")).appendTo(section);
			for (const row of data.items) {
				const title = row.subject || row.target_name || row.reference_name || row.name;
				$("<button class='btn btn-link d-block'>")
					.text(title)
					.on("click", () => frappe.set_route("Form", dt, row.name))
					.appendTo(section);
				if (row.resolution_due_at)
					$("<small>")
						.text(__("解决期限") + ": " + row.resolution_due_at)
						.appendTo(section);
			}
			if (data.has_more)
				$("<p class='text-muted'>")
					.text(__("还有候选事项，请进入对应查询并缩小条件查看。"))
					.appendTo(section);
		}
		$("<h4>").text(__("核心报表")).appendTo(body);
		for (const item of message.reports)
			$("<button class='btn btn-default m-1'>")
				.text(item.code + " " + __(item.label))
				.on("click", () => frappe.set_route("query-report", item.name, context()))
				.appendTo(body);
	};
	const register = (row = null) => {
		const dialog = new frappe.ui.Dialog({
			title: __(row ? "维护医院主数据登记" : "登记医院使用的主数据"),
			fields: [
				{
					fieldname: "company",
					label: __("法人"),
					fieldtype: "Link",
					options: "Company",
					reqd: 1,
					default: row?.company || context().company,
				},
				{
					fieldname: "hospital",
					label: __("医院"),
					fieldtype: "Link",
					options: "HRP Hospital",
					reqd: 1,
					default: row?.hospital || context().hospital,
				},
				{
					fieldname: "organization_unit",
					label: __("科室或组织"),
					fieldtype: "Link",
					options: "HRP Organization Unit",
					default: row?.organization_unit || context().organization_unit,
				},
				{
					fieldname: "master_data_domain",
					label: __("主数据域"),
					fieldtype: "Link",
					options: "HRP Master Data Domain",
					reqd: 1,
					default: row?.master_data_domain,
				},
				{
					fieldname: "target_name",
					label: __("标准主数据编号"),
					fieldtype: "Data",
					reqd: 1,
					default: row?.target_name,
				},
				{ fieldname: "enabled", label: __("启用登记"), fieldtype: "Check", default: row ? row.enabled : 1 },
				{
					fieldname: "valid_from",
					label: __("生效日期"),
					fieldtype: "Date",
					reqd: 1,
					default: row?.valid_from || frappe.datetime.get_today(),
				},
				{ fieldname: "valid_to", label: __("失效日期"), fieldtype: "Date", default: row?.valid_to },
			],
			primary_action_label: __("保存登记"),
			primary_action: async (values) => {
				const scope = Object.fromEntries(
					["company", "hospital", "organization_unit", "master_data_domain"].map((key) => [
						key,
						values[key] || null,
					]),
				);
				const entries = [
					{
						target_name: values.target_name,
						enabled: values.enabled,
						valid_from: values.valid_from,
						valid_to: values.valid_to || null,
						expected_revision: row?.revision || 0,
					},
				];
				await frappe.call({
					method: "medops.api.v1.core_workbench.register_population",
					args: { scope, entries },
					headers: { "Idempotency-Key": dialog.registration_key },
					freeze: true,
				});
				dialog.hide();
				frappe.show_alert({ message: __("登记已保存"), indicator: "green" });
			},
		});
		dialog.registration_key = "population-" + crypto.randomUUID();
		if (row)
			for (const key of ["company", "hospital", "organization_unit", "master_data_domain", "target_name"])
				dialog.set_df_property(key, "read_only", 1);
		dialog.show();
	};
	page.set_primary_action(__("刷新"), load);
	page.add_menu_item(__("主数据受控查询"), () => frappe.set_route("hrp-master-data-access"));
	if (writable) page.add_menu_item(__("登记主数据"), () => register());
	page.add_menu_item(__("查看及维护登记"), async () => {
		const { message } = await frappe.call({
			method: "medops.api.v1.core_workbench.population",
			type: "GET",
			args: { filters: JSON.stringify(context()) },
			freeze: true,
		});
		const dialog = new frappe.ui.Dialog({
			title: __("当前可见登记"),
			fields: [{ fieldname: "list", fieldtype: "HTML" }],
		});
		const list = dialog.fields_dict.list.$wrapper;
		if (!message.items.length) $("<p>").text(__("没有可见登记；这不代表全院目录为空。")).appendTo(list);
		for (const row of message.items)
			$("<button class='btn btn-link d-block'>")
				.text(
					row.target_name + " · " + row.master_data_domain + " · " + (row.enabled ? __("启用") : __("停用")),
				)
				.on("click", () => {
					dialog.hide();
					if (writable) register(row);
					else frappe.set_route("Form", "HRP Reference Link", row.name);
				})
				.appendTo(list);
		dialog.show();
	});
	load();
};
