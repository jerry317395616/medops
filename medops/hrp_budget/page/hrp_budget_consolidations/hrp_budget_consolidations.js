frappe.pages["hrp-budget-consolidations"].on_page_load = (wrapper) => {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("预算汇总"), single_column: true });
	const filters = $("<div class='row mt-3'>").appendTo(page.body);
	const summary = $("<p class='text-muted mt-3'>").appendTo(page.body);
	const body = $("<div class='mt-3'>").appendTo(page.body);
	const controls = {},
		selected = new Map();
	const manager = ["System Manager", "HRP System Manager", "HRP Budget Manager"].some((role) =>
		frappe.user.has_role(role),
	);
	const writer =
		manager || ["HRP Budget User", "HRP Finance User"].some((role) => frappe.user.has_role(role));
	let offset = 0,
		generation = 0,
		ready = false,
		previousSignature,
		previousKey,
		listMode = "eligible";
	const call = async (module, method, args, key) =>
		(
			await frappe.call({
				method: `medops.api.v1.finance.${module}.${method}`,
				args,
				type: key ? "POST" : "GET",
				headers: key ? { "Idempotency-Key": key } : undefined,
				freeze: true,
			})
		).message;
	const context = () =>
		Object.fromEntries(
			["company", "hospital", "organization_version", "fiscal_year"]
				.map((key) => [key, controls[key].get_value()])
				.filter(([, value]) => value),
		);
	function table(parent, headings) {
		const element = $("<table class='table table-sm table-bordered'>").appendTo(
			$("<div class='table-responsive'>").appendTo(parent),
		);
		const row = $("<tr>").appendTo($("<thead>").appendTo(element));
		for (const label of headings) $("<th class='text-nowrap'>").text(__(label)).appendTo(row);
		return $("<tbody>").appendTo(element);
	}
	function button(parent, label, callback) {
		return $("<button class='btn btn-default mr-2 mb-2'>")
			.text(__(label))
			.on("click", callback)
			.appendTo(parent);
	}
	function selectionSummary() {
		summary.text(
			__("已选择 {0} 个已批准科室申报。封存保留全部原始明细，随后需独立人工审批。", [selected.size]),
		);
	}
	async function approval(doc) {
		const rows = await call("approval", "list_records", { filters: { submission: doc.name }, limit: 1 });
		const latest = rows.items[0];
		if (latest && ["Pending", "Approved"].includes(latest.status))
			return frappe.set_route("hrp-budget-approvals", latest.name);
		if (!writer) return frappe.msgprint(__("当前没有可见审批单。"));
		const key = `budget-approval-${frappe.utils.get_random(24)}`;
		frappe.confirm(__("将封存汇总提交独立人工审批，确认发起？"), async () => {
			const result = await call(
				"approval",
				"request_case",
				{ payload: { submission: doc.name, expected_revision: doc.revision } },
				key,
			);
			frappe.set_route("hrp-budget-approvals", result.name);
		});
	}
	async function showRecord(name) {
		const current = ++generation;
		filters.hide();
		summary.empty();
		body.empty();
		const doc = await call("submission", "get_record", { name });
		if (current !== generation) return;
		$("<h3>").text(doc.subject).appendTo(body);
		$("<p>")
			.text(`${doc.currency} ${doc.total_amount} · ${doc.posting_date} · ${doc.applicant}`)
			.appendTo(body);
		$("<p>").text(__("已封存事实；批准和预算下达通过后续流程执行。")).appendTo(body);
		button(body, "查看审批 / 发起审批", () => approval(doc));
		if (doc.submission_type === "Consolidated") {
			$("<h4>").text(__("已批准来源")).appendTo(body);
			const rows = table(body, ["原始计划", "原编制人", "原申请人", "科室审批"]);
			for (const source of doc.sources) {
				const row = $("<tr>").appendTo(rows);
				for (const value of [source.origin_plan, source.preparer, source.requested_by])
					$("<td>").text(value).appendTo(row);
				button($("<td>").appendTo(row), "查看来源审批", () =>
					frappe.set_route("hrp-budget-approvals", source.approval_case),
				);
			}
		}
		const detail = $("<div>").appendTo(body);
		const ordered = [...doc.lines].sort(
			(a, b) =>
				a.organization_unit.localeCompare(b.organization_unit) ||
				a.period_index - b.period_index ||
				a.fact_key.localeCompare(b.fact_key),
		);
		function lines(start = 0) {
			detail.empty();
			$("<h4>")
				.text(__("原始明细：共 {0} 条", [ordered.length]))
				.appendTo(detail);
			const rows = table(detail, [
				"原始科室",
				"成本中心",
				"预算科目",
				"资金来源",
				"会计科目",
				"项目",
				"期间开始",
				"期间结束",
				"精确金额",
			]);
			for (const item of ordered.slice(start, start + 200)) {
				const row = $("<tr>").appendTo(rows);
				for (const field of [
					"organization_unit",
					"cost_center",
					"budget_subject",
					"funding_source",
					"account",
					"project",
					"period_start",
					"period_end",
					"amount",
				])
					$("<td>")
						.text(doc.display_labels?.[field]?.[item[field]] || item[field] || "")
						.appendTo(row);
			}
			if (start > 0) button(detail, "上一页明细", () => lines(Math.max(0, start - 200)));
			if (start + 200 < ordered.length) button(detail, "下一页明细", () => lines(start + 200));
		}
		lines();
	}
	async function refresh() {
		const name = frappe.get_route()[1];
		if (name) return showRecord(name);
		if (listMode === "sealed") return closed();
		const current = ++generation;
		filters.show();
		body.empty();
		selectionSummary();
		if (Object.keys(context()).length < 4)
			return $("<p>").text(__("先选择法人、医院、预算年度和组织快照，查询已批准科室申报。")).appendTo(body);
		const result = await call("consolidation", "eligible", { filters: context(), offset, limit: 50 });
		if (current !== generation) return;
		const rows = table(body, ["选择", "科室", "计划", "年度", "币种 / 精度", "金额", "明细数"]);
		for (const item of result.items) {
			const row = $("<tr>").appendTo(rows);
			$("<input type='checkbox'>")
				.attr("aria-label", `${__("选择科室申报")} ${item.submission}`)
				.prop("checked", selected.has(item.submission))
				.on("change", function () {
					if (this.checked && selected.size >= 1000) {
						this.checked = false;
						return frappe.msgprint(__("一次最多选择 1000 个科室申报。"));
					}
					if (this.checked) selected.set(item.submission, item);
					else selected.delete(item.submission);
					selectionSummary();
				})
				.appendTo($("<td>").appendTo(row));
			for (const value of [
				item.organization_unit,
				item.subject,
				item.fiscal_year,
				`${item.currency} / ${item.currency_precision}`,
				item.total_amount,
				item.line_count,
			])
				$("<td>").text(value).appendTo(row);
		}
		if (!result.items.length) $("<p>").text(__("当前范围没有可见的已批准科室申报。")).appendTo(body);
		if (offset > 0)
			button(body, "上一页", () => {
				offset = Math.max(0, offset - 50);
				refresh();
			});
		if (result.next_offset !== null)
			button(body, "下一页", () => {
				offset = result.next_offset;
				refresh();
			});
		if (manager) button(body, "封存选中汇总", seal);
	}
	async function seal() {
		const organization_unit = controls.organization_unit.get_value(),
			subject = controls.subject.get_value();
		if (!organization_unit || !subject || !selected.size)
			return frappe.msgprint(__("请选择汇总组织、填写名称并选择来源。"));
		const sources = [...selected.values()]
			.sort((a, b) => a.submission.localeCompare(b.submission))
			.map((row) =>
				Object.fromEntries(
					[
						"submission",
						"expected_revision",
						"approval_case",
						"expected_approval_revision",
						"snapshot_digest",
					].map((key) => [key, row[key]]),
				),
			);
		const payload = { organization_unit, subject, sources };
		const signature = JSON.stringify(payload);
		if (signature !== previousSignature) {
			previousSignature = signature;
			previousKey = `budget-consolidation-${frappe.utils.get_random(24)}`;
		}
		frappe.confirm(
			__("将选中来源按原始科室、期间和资金维度封存。封存后仍需独立审批，确认生成？"),
			async () => {
				const result = await call("consolidation", "seal", { payload }, previousKey);
				frappe.set_route("hrp-budget-consolidations", result.name);
			},
		);
	}
	for (const df of [
		{ fieldname: "company", label: __("法人"), fieldtype: "Link", options: "Company" },
		{
			fieldname: "hospital",
			label: __("医院"),
			fieldtype: "Link",
			options: "HRP Hospital",
			get_query: () => ({ filters: { company: controls.company.get_value() } }),
		},
		{ fieldname: "fiscal_year", label: __("预算年度"), fieldtype: "Link", options: "Fiscal Year" },
		{
			fieldname: "organization_version",
			label: __("组织快照"),
			fieldtype: "Link",
			options: "HRP Organization Version",
			get_query: () => ({
				filters: {
					company: controls.company.get_value(),
					hospital: controls.hospital.get_value(),
					status: "Published",
					docstatus: 1,
				},
			}),
		},
		{
			fieldname: "organization_unit",
			label: __("汇总组织"),
			fieldtype: "Link",
			options: "HRP Organization Unit",
			get_query: () => ({
				filters: {
					organization_version: controls.organization_version.get_value(),
					hospital: controls.hospital.get_value(),
				},
			}),
		},
		{ fieldname: "subject", label: __("汇总名称"), fieldtype: "Data" },
	]) {
		controls[df.fieldname] = frappe.ui.form.make_control({
			parent: $("<div class='col-md-4'>").appendTo(filters),
			df: {
				...df,
				onchange: () => {
					if (!ready || ["organization_unit", "subject"].includes(df.fieldname)) return;
					selected.clear();
					offset = 0;
					if (["company", "hospital", "organization_version"].includes(df.fieldname))
						controls.organization_unit.set_value("");
					if (df.fieldname === "company" && controls.hospital.get_value())
						return controls.hospital.set_value("");
					if (["company", "hospital"].includes(df.fieldname) && controls.organization_version.get_value())
						return controls.organization_version.set_value("");
					refresh();
				},
			},
			render_input: true,
		});
	}
	ready = true;
	function mode(value) {
		listMode = value;
		offset = 0;
		return frappe.get_route()[1] ? frappe.set_route("hrp-budget-consolidations") : refresh();
	}
	async function closed() {
		const current = ++generation;
		filters.show();
		body.empty();
		summary.empty();
		if (Object.keys(context()).length < 4)
			return $("<p>").text(__("先选择法人、医院、预算年度和组织快照。")).appendTo(body);
		const result = await call("submission", "list_records", {
			filters: { ...context(), submission_type: "Consolidated" },
			offset,
			limit: 50,
		});
		if (current !== generation) return;
		for (const doc of result.items)
			button(body, `${doc.subject} · ${doc.currency} ${doc.total_amount}`, () =>
				frappe.set_route("hrp-budget-consolidations", doc.name),
			);
		if (!result.items.length) $("<p>").text(__("没有可见封存汇总。")).appendTo(body);
		if (offset > 0)
			button(body, "上一页", () => {
				offset = Math.max(0, offset - 50);
				refresh();
			});
		if (result.next_offset !== null)
			button(body, "下一页", () => {
				offset = result.next_offset;
				refresh();
			});
	}
	page.add_inner_button(__("选择已批准科室申报"), () => mode("eligible"));
	page.add_inner_button(__("查看封存汇总"), () => mode("sealed"));
	page.add_inner_button(__("刷新"), refresh);
	wrapper.budgetConsolidationsRefresh = refresh;
};
frappe.pages["hrp-budget-consolidations"].on_page_show = (wrapper) => {
	frappe.breadcrumbs.add({
		type: "Custom",
		label: __("预算汇总"),
		route: frappe.router.make_url(["hrp-budget-consolidations"]),
	});
	return wrapper.budgetConsolidationsRefresh?.();
};
