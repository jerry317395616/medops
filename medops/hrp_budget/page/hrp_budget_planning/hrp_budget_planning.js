frappe.pages["hrp-budget-planning"].on_page_load = (wrapper) => {
	const methodPrefix = "medops.api.v1.finance.planning.";
	const statusLabel = (value) =>
		({ Draft: __("草稿"), Frozen: __("已冻结"), Withdrawn: __("已撤回") })[value] || value;
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("预算编制"), single_column: true });
	const has = (...roles) => roles.some((role) => frappe.user.has_role(role));
	const manager = has("System Manager", "HRP System Manager", "HRP Budget Manager");
	const writer = manager || has("HRP Budget User", "HRP Finance User");
	const steward = has("System Manager", "HRP System Manager", "HRP Data Steward");
	const call = async (method, args, headers) =>
		(
			await frappe.call({
				method: methodPrefix + method,
				args,
				headers,
				type: headers ? "POST" : "GET",
				freeze: true,
			})
		).message;
	const command = () => {
		let previous, key;
		return (method, args) => {
			const signature = JSON.stringify([method, args]);
			if (signature !== previous) {
				previous = signature;
				key = `budget-${frappe.utils.get_random(24)}`;
			}
			return call(method, args, { "Idempotency-Key": key });
		};
	};
	const submissionCall = async (method, args, headers) =>
		(
			await frappe.call({
				method: "medops.api.v1.finance.submission." + method,
				args,
				headers,
				type: headers ? "POST" : "GET",
				freeze: true,
			})
		).message;
	async function showSubmission(name) {
		const doc = await submissionCall("get_record", { name });
		const dialog = new frappe.ui.Dialog({
			title: __("科室预算申报"),
			size: "extra-large",
			fields: [{ fieldname: "facts", fieldtype: "HTML" }],
		});
		const content = dialog.fields_dict.facts.$wrapper.empty();
		$("<h4>").text(doc.subject).appendTo(content);
		$("<p>").text(__("已封存，待人工审批；当前申报不形成可用预算。")).appendTo(content);
		$("<p>")
			.text(`${doc.currency} ${doc.total_amount} · ${doc.posting_date} · ${doc.applicant}`)
			.appendTo(content);
		const proof = $("<details>").appendTo(content);
		$("<summary>").text(__("来源与封存凭证")).appendTo(proof);
		$("<p>")
			.text(`${__("申报事实摘要")}：${doc.snapshot_digest}`)
			.appendTo(proof);
		const tableContainer = $("<div class='table-responsive'>").appendTo(content);
		const rows = table(tableContainer, [
			"原始科室",
			"预算科目",
			"资金来源",
			"会计科目",
			"项目",
			"期间开始",
			"期间结束",
			"精确金额",
		]);
		const displayLines = [...doc.lines].sort(
			(a, b) => a.period_index - b.period_index || a.fact_key.localeCompare(b.fact_key),
		);
		for (const row of displayLines) {
			const tr = $("<tr>").appendTo(rows);
			for (const field of [
				"organization_unit",
				"budget_subject",
				"funding_source",
				"account",
				"project",
				"period_start",
				"period_end",
				"amount",
			]) {
				$("<td>")
					.text(doc.display_labels?.[field]?.[row[field]] || row[field] || "")
					.appendTo(tr);
			}
		}
		const approvalButton = $("<button class='btn btn-primary mt-3'>")
			.text(__("查看审批 / 发起审批"))
			.on("click", async () => {
				const method = "medops.api.v1.finance.approval.";
				const existing = (
					await frappe.call({
						method: method + "list_records",
						args: { filters: { submission: doc.name }, limit: 1 },
						type: "GET",
					})
				).message;
				const latest = existing.items[0];
				if (latest && ["Pending", "Approved"].includes(latest.status)) {
					dialog.hide();
					return frappe.set_route("hrp-budget-approval", latest.name);
				}
				if (!writer) return frappe.msgprint(__("当前没有可见审批单。"));
				const key = `budget-approval-${frappe.utils.get_random(24)}`;
				frappe.confirm(__("将当前封存申报提交独立审批，确认发起？"), async () => {
					const result = (
						await frappe.call({
							method: method + "request_case",
							args: { payload: { submission: doc.name, expected_revision: doc.revision } },
							headers: { "Idempotency-Key": key },
							type: "POST",
							freeze: true,
						})
					).message;
					dialog.hide();
					frappe.set_route("hrp-budget-approval", result.name);
				});
			});
		approvalButton.appendTo(content);
		dialog.show();
	}
	async function openSubmission(plan) {
		const existing = await submissionCall("list_records", { filters: { source_plan: plan.name }, limit: 1 });
		if (existing.items.length) return showSubmission(existing.items[0].name);
		if (!writer || plan.status !== "Draft") return frappe.msgprint(__("当前计划还没有可见申报。"));
		const key = `budget-submission-${frappe.utils.get_random(24)}`;
		frappe.confirm(__("将冻结计划封存为科室申报；封存后不能修改，后续需人工审批。确认生成？"), async () => {
			const result = await submissionCall(
				"seal_department",
				{ payload: { source_plan: plan.name, expected_revision: plan.revision } },
				{ "Idempotency-Key": key },
			);
			await showSubmission(result.name);
		});
	}
	const body = $("<div class='mt-3'>");
	const fields = {},
		filters = $("<div class='row'>").appendTo(page.body);
	let selected = null,
		generation = 0;
	const link = (fieldname, label, options, query) => ({
		fieldname,
		label: __(label),
		fieldtype: "Link",
		options,
		reqd: 1,
		get_query: query,
	});
	const context = () =>
		Object.fromEntries(
			Object.entries(fields)
				.map(([k, v]) => [k, v.get_value()])
				.filter(([, v]) => v),
		);
	const masterQuery = (_doctype, extra = {}) => ({
		query: methodPrefix + "search",
		filters: { ...context(), ...extra },
	});
	for (const df of [
		link("company", "法人", "Company"),
		link("hospital", "医院", "HRP Hospital", () => ({ filters: { company: fields.company.get_value() } })),
	]) {
		fields[df.fieldname] = frappe.ui.form.make_control({
			parent: $("<div class='col-md-6'>").appendTo(filters),
			df: {
				...df,
				onchange: () => {
					selected = null;
					generation++;
					body.empty();
					if (df.fieldname === "company") fields.hospital?.set_value("");
				},
			},
			render_input: true,
		});
	}
	$("<p class='text-muted mt-3'>")
		.text(
			__(
				"按医院和组织快照编制预算。冻结后保留编制事实，后续调整需复制新版本。冻结不代表财务审批或预算可执行额度。",
			),
		)
		.appendTo(page.body);
	body.appendTo(page.body);
	const requireContext = () => {
		if (!fields.company.get_value() || !fields.hospital.get_value()) {
			frappe.msgprint(__("请先选择法人和医院。"));
			return false;
		}
		return true;
	};
	const button = (parent, label, action) =>
		$("<button class='btn btn-default btn-sm mr-2 mb-2' type='button'>")
			.text(__(label))
			.on("click", action)
			.appendTo(parent);
	const table = (parent, headers) => {
		const t = $("<table class='table table-bordered'>").appendTo(
			$("<div class='table-responsive'>").appendTo(parent),
		);
		const tr = $("<tr>").appendTo($("<thead>").appendTo(t));
		headers.forEach((h) => $("<th>").text(__(h)).appendTo(tr));
		return $("<tbody>").appendTo(t);
	};
	async function versions(offset = 0) {
		if (!requireContext()) return;
		selected = null;
		const token = ++generation;
		const result = await call("list_records", { kind: "version", filters: context(), offset });
		if (token !== generation) return;
		body.empty();
		$("<h4>").text(__("预算版本")).appendTo(body);
		const tb = table(body, ["版本", "名称", "财政年度", "状态", "操作"]);
		for (const row of result.items) {
			const tr = $("<tr>").appendTo(tb);
			[row.name, row.subject, row.fiscal_year, statusLabel(row.status)].forEach((v) =>
				$("<td>").text(v).appendTo(tr),
			);
			button($("<td>").appendTo(tr), "打开", () => openVersion(row.name));
		}
		if (!result.items.length) $("<p>").text(__("当前范围没有可见的预算版本。")).appendTo(body);
		if (offset) button(body, "上一页", () => versions(Math.max(0, offset - 50)));
		if (result.next_offset != null) button(body, "下一页", () => versions(result.next_offset));
	}
	async function openVersion(name, offset = 0) {
		const token = ++generation;
		const version = await call("get_record", { kind: "version", name });
		const result = await call("list_records", { kind: "plan", filters: { budget_version: name }, offset });
		if (token !== generation) return;
		selected = version;
		body.empty();
		$("<h4>")
			.text(`${version.subject} · ${statusLabel(version.status)}`)
			.appendTo(body);
		$("<p>")
			.text(
				`${version.name} · ${version.fiscal_year} · ${version.period_start} — ${version.period_end} · ${version.currency} · ${__("修订")} ${version.revision}`,
			)
			.appendTo(body);
		const toolbar = $("<div>").appendTo(body);
		button(toolbar, "返回版本列表", () => versions());
		if (manager && version.status === "Draft") {
			button(toolbar, "修改版本说明", () => versionDialog(version));
			const write = command();
			button(toolbar, "冻结编制版本", () =>
				frappe.confirm(__("冻结会锁定所有计划和金额；这一步不会完成财务审批。确认冻结？"), async () => {
					await write("freeze_version", { payload: { name, expected_revision: version.revision } });
					await openVersion(name);
				}),
			);
		}
		if (manager && version.status === "Frozen") {
			const write = command();
			button(toolbar, "复制为新版本", () =>
				frappe.confirm(__("将当前有效计划复制为新的草稿版本，继续修订？"), async () => {
					const result = await write("clone_version", {
						payload: { name, expected_revision: version.revision },
					});
					await openVersion(result.name);
				}),
			);
		}
		if (writer && version.status === "Draft") button(toolbar, "新增科室计划", () => planDialog(version));
		const tb = table(body, ["计划名称", "状态", "操作"]);
		for (const row of result.items) {
			const tr = $("<tr>").appendTo(tb);
			[row.subject, statusLabel(row.status)].forEach((v) => $("<td>").text(v).appendTo(tr));
			const actions = $("<td>").appendTo(tr);
			button(actions, "查看计划", async () =>
				planDialog(version, await call("get_record", { kind: "plan", name: row.name })),
			);
			if (version.status === "Frozen") button(actions, "预算申报", () => openSubmission(row));
		}
		if (!result.items.length) $("<p>").text(__("当前版本没有可见的科室计划。")).appendTo(body);
		if (offset) button(body, "上一页", () => openVersion(name, Math.max(0, offset - 50)));
		if (result.next_offset != null) button(body, "下一页", () => openVersion(name, result.next_offset));
	}
	function versionDialog(doc) {
		if (!requireContext()) return;
		const write = command();
		const fixed = [
			link("company", "法人", "Company"),
			link("hospital", "医院", "HRP Hospital"),
			link("organization_version", "组织快照", "HRP Organization Version", () => ({
				filters: { ...context(), docstatus: 1, status: "Published" },
			})),
			link("fiscal_year", "财政年度", "Fiscal Year"),
			{
				fieldname: "distribution_frequency",
				label: __("编制期间"),
				fieldtype: "Select",
				options: "Monthly\nQuarterly\nHalf-Yearly\nYearly",
				default: "Monthly",
				reqd: 1,
			},
		];
		const dialog = new frappe.ui.Dialog({
			title: __(doc ? "修改版本说明" : "新建预算版本"),
			fields: [
				...fixed.map((df) => ({
					...df,
					default: doc?.[df.fieldname] || context()[df.fieldname] || df.default,
					read_only: !!doc || ["company", "hospital"].includes(df.fieldname),
				})),
				{ fieldname: "subject", label: __("版本名称"), fieldtype: "Data", reqd: 1, default: doc?.subject },
				{ fieldname: "remarks", label: __("说明"), fieldtype: "Small Text", default: doc?.remarks },
			],
			primary_action_label: __("保存"),
			primary_action: async (values) => {
				const result = await write("save_version", {
					payload: { ...values, ...(doc ? { name: doc.name, expected_revision: doc.revision } : {}) },
				});
				dialog.hide();
				await openVersion(result.name);
			},
		});
		dialog.show();
	}
	function lineFields() {
		return [
			{
				...link("budget_subject", "预算科目", "HRP Dimension Value", () =>
					masterQuery("HRP Dimension Value", { enabled: 1 }),
				),
				in_list_view: 1,
				columns: 3,
			},
			{
				...link("funding_source", "资金来源", "HRP Funding Source", () =>
					masterQuery("HRP Funding Source", { enabled: 1 }),
				),
				in_list_view: 1,
				columns: 2,
			},
			{
				...link("account", "财务科目", "Account", () => ({
					filters: { company: selected.company, is_group: 0, report_type: "Profit and Loss", disabled: 0 },
				})),
				in_list_view: 1,
				columns: 3,
			},
			{ ...link("project", "项目", "Project", () => ({ filters: { company: selected.company } })), reqd: 0 },
			{
				fieldname: "period_index",
				label: __("期间序号"),
				fieldtype: "Int",
				reqd: 1,
				in_list_view: 1,
				columns: 1,
			},
			{
				fieldname: "amount",
				label: __("金额"),
				fieldtype: "Data",
				reqd: 1,
				in_list_view: 1,
				columns: 2,
				description: __("输入十进制金额"),
			},
			{ fieldname: "description", label: __("说明"), fieldtype: "Small Text" },
		];
	}
	function planDialog(version, doc) {
		const editable = writer && version.status === "Draft",
			write = command();
		const columns = lineFields();
		const dialog = new frappe.ui.Dialog({
			title: __(doc ? "科室预算计划" : "新增科室预算计划"),
			size: "extra-large",
			fields: [
				{
					...link("organization_unit", "编制科室", "HRP Organization Unit", () => ({
						filters: { organization_version: version.organization_version, enabled: 1, is_group: 0 },
					})),
					default: doc?.organization_unit,
					read_only: !!doc || !editable,
				},
				{
					fieldname: "subject",
					label: __("计划名称"),
					fieldtype: "Data",
					reqd: 1,
					default: doc?.subject,
					read_only: !editable,
				},
				{
					fieldname: "remarks",
					label: __("说明"),
					fieldtype: "Small Text",
					default: doc?.remarks,
					read_only: !editable,
				},
				{ fieldname: "periods", fieldtype: "HTML" },
				{
					fieldname: "lines",
					label: __("预算明细"),
					fieldtype: "Table",
					fields: columns,
					data: doc?.lines || [],
					in_place_edit: true,
					cannot_add_rows: !editable,
					cannot_delete_rows: !editable,
					read_only: !editable,
				},
			],
			primary_action_label: __(editable ? "保存草稿" : "关闭"),
			primary_action: async (values) => {
				if (!editable) {
					dialog.hide();
					return;
				}
				const payload = {
					budget_version: version.name,
					organization_unit: values.organization_unit,
					subject: values.subject,
					remarks: values.remarks,
					...(doc ? { name: doc.name, expected_revision: doc.revision } : {}),
					lines: (values.lines || []).map((row) =>
						Object.fromEntries(columns.map((df) => [df.fieldname, row[df.fieldname] ?? null])),
					),
				};
				await write("save_plan", { payload });
				dialog.hide();
				await openVersion(version.name);
			},
		});
		const info = dialog.fields_dict.periods.$wrapper;
		$("<p>")
			.text(
				`${__("记账币种")}: ${version.currency} · ${__("金额精度")}: ${version.currency_precision}${doc ? ` · ${__("合计")}: ${doc.total_amount} · ${statusLabel(doc.status)}` : ""}`,
			)
			.appendTo(info);
		$("<p class='text-muted'>")
			.text(version.periods.map((p) => `${p.period_index}: ${p.period_start} — ${p.period_end}`).join("；"))
			.appendTo(info);
		if (editable)
			button(info, "按期间均分新增明细", () => {
				const spread = new frappe.ui.Dialog({
					title: __("按期间均分"),
					fields: [
						...columns.filter((df) => !["period_index", "amount", "description"].includes(df.fieldname)),
						{ fieldname: "total", label: __("年度金额"), fieldtype: "Data", reqd: 1 },
					],
					primary_action_label: __("添加明细"),
					primary_action: async (values) => {
						const result = await call("spread", { version: version.name, amount: values.total });
						const grain = {
							budget_subject: values.budget_subject,
							funding_source: values.funding_source,
							account: values.account,
							project: values.project || null,
						};
						const grid = dialog.fields_dict.lines;
						grid.df.data = [
							...(grid.df.data || []),
							...result.items.map((row) => ({
								...grain,
								period_index: row.period_index,
								amount: row.amount,
							})),
						];
						grid.grid.refresh();
						spread.hide();
					},
				});
				spread.show();
			});
		if (editable && doc?.status === "Draft")
			button(info, "撤回计划", () =>
				frappe.confirm(__("撤回后明细仍会保留，不再纳入版本编制。确认撤回？"), async () => {
					await write("withdraw_plan", { payload: { name: doc.name, expected_revision: doc.revision } });
					dialog.hide();
					await openVersion(version.name);
				}),
			);
		if (doc)
			button(info, "导出明细", async () => {
				const result = await call("export_plan", { name: doc.name });
				const quote = (value) => {
					let s = String(value ?? "");
					if (/^[\s]*[=+@-]/.test(s)) s = "'" + s;
					return `"${s.replaceAll('"', '""')}"`;
				};
				const data = [result.columns, ...result.rows].map((row) => row.map(quote).join(",")).join("\r\n");
				const url = URL.createObjectURL(new Blob(["\ufeff" + data], { type: "text/csv;charset=utf-8" }));
				const a = document.createElement("a");
				a.href = url;
				a.download = "budget-plan.csv";
				a.click();
				URL.revokeObjectURL(url);
			});
		dialog.show();
	}
	const masterKinds = { dimension: "业务维度", value: "预算科目", fund: "资金来源" };
	async function masterList(kind, offset = 0) {
		if (!requireContext()) return;
		selected = null;
		const token = ++generation;
		const result = await call("list_records", { kind, filters: context(), offset });
		if (token !== generation) return;
		body.empty();
		$("<h4>").text(__(masterKinds[kind])).appendTo(body);
		if (steward) button(body, "新增", () => masterDialog(kind));
		const tb = table(body, ["编码", "名称", "启用", "生效日期", "操作"]);
		for (const row of result.items) {
			const tr = $("<tr>").appendTo(tb);
			[row.code, row.display_name, row.enabled ? __("是") : __("否"), row.valid_from].forEach((v) =>
				$("<td>").text(v).appendTo(tr),
			);
			if (steward) button($("<td>").appendTo(tr), "维护", () => masterDialog(kind, row));
		}
		if (offset) button(body, "上一页", () => masterList(kind, Math.max(0, offset - 50)));
		if (result.next_offset != null) button(body, "下一页", () => masterList(kind, result.next_offset));
	}
	function masterDialog(kind, doc) {
		const write = command();
		const extra =
			kind === "dimension"
				? {
						fieldname: "purpose",
						label: __("用途"),
						fieldtype: "Select",
						options: [
							{ value: "Budget Subject", label: __("预算科目") },
							{ value: "Analytical", label: __("统计分析") },
						],
						default: "Budget Subject",
						read_only: !!doc,
					}
				: kind === "value"
					? link("dimension", "所属维度", "HRP Business Dimension", () =>
							masterQuery("HRP Business Dimension", { enabled: 1 }),
						)
					: {
							fieldname: "source_type",
							label: __("资金类型"),
							fieldtype: "Select",
							options: [
								{ value: "", label: "" },
								{ value: "Fiscal Appropriation", label: __("财政拨款") },
								{ value: "Own Revenue", label: __("自有收入") },
								{ value: "Grant", label: __("专项资助") },
								{ value: "Other", label: __("其他") },
							],
						};
		const dfs = [
			{ fieldname: "code", label: __("编码"), fieldtype: "Data", reqd: 1, read_only: !!doc },
			{ fieldname: "display_name", label: __("名称"), fieldtype: "Data", reqd: 1 },
			{ ...extra, reqd: 1, read_only: !!doc && kind !== "fund" },
			{ fieldname: "enabled", label: __("启用"), fieldtype: "Check", default: 1 },
			{ fieldname: "valid_from", label: __("生效日期"), fieldtype: "Date", reqd: 1 },
			{ fieldname: "valid_to", label: __("失效日期"), fieldtype: "Date" },
			{ fieldname: "remarks", label: __("说明"), fieldtype: "Small Text" },
		];
		const dialog = new frappe.ui.Dialog({
			title: __(masterKinds[kind]),
			fields: dfs.map((df) => ({ ...df, default: doc?.[df.fieldname] ?? df.default })),
			primary_action_label: __("保存"),
			primary_action: async (values) => {
				await write("upsert_master", {
					kind,
					payload: { ...context(), ...values, expected_revision: doc?.revision || 0 },
				});
				dialog.hide();
				await masterList(kind);
			},
		});
		dialog.show();
	}
	page.set_primary_action(__("查询预算版本"), () => versions());
	if (manager) page.set_secondary_action(__("新建预算版本"), () => versionDialog());
	Object.entries(masterKinds).forEach(([kind, label]) =>
		page.add_menu_item(__(label), () => masterList(kind)),
	);
};
